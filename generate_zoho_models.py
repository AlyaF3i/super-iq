import argparse
import json
import keyword
import re
from pathlib import Path
from typing import Any


HEADER = '''"""Generated SQLAlchemy models for a local Zoho CRM mirror.

Do not edit by hand. Regenerate with:
    python generate_zoho_models.py --schema zoho_schema.json --output zoho_crm_models.py
"""

from sqlalchemy import Boolean, Column, Date, DateTime, Float, Integer, JSON, String, Text
from sqlalchemy.orm import declarative_base


Base = declarative_base()

'''


TYPE_MAP = {
    "boolean": "Boolean",
    "integer": "Integer",
    "bigint": "Integer",
    "long": "Integer",
    "double": "Float",
    "currency": "Float",
    "percent": "Float",
    "date": "Date",
    "datetime": "DateTime",
    "lookup": "JSON",
    "ownerlookup": "JSON",
    "multiselectlookup": "JSON",
    "multiuserlookup": "JSON",
    "subform": "JSON",
    "fileupload": "JSON",
    "imageupload": "JSON",
    "profileimage": "JSON",
    "multiselectpicklist": "JSON",
    "multi_select_picklist": "JSON",
    "jsonobject": "JSON",
    "jsonarray": "JSON",
    "textarea": "Text",
}


def class_name(name: str) -> str:
    parts = re.split(r"[^0-9A-Za-z]+", name)
    cleaned = "".join(part[:1].upper() + part[1:] for part in parts if part)
    if not cleaned:
        cleaned = "ZohoModule"
    if cleaned[0].isdigit():
        cleaned = f"Zoho{cleaned}"
    return cleaned


def attr_name(name: str, used: set[str]) -> str:
    cleaned = re.sub(r"\W+", "_", name).strip("_").lower()
    if not cleaned:
        cleaned = "field"
    if cleaned[0].isdigit():
        cleaned = f"field_{cleaned}"
    if keyword.iskeyword(cleaned):
        cleaned = f"{cleaned}_"

    base = cleaned
    suffix = 2
    while cleaned in used:
        cleaned = f"{base}_{suffix}"
        suffix += 1
    used.add(cleaned)
    return cleaned


def column_type(field: dict[str, Any]) -> str:
    data_type = str(field.get("data_type") or field.get("json_type") or "").lower().replace(" ", "")
    api_name = str(field.get("api_name") or "").lower()
    max_length = field.get("length") or field.get("maximum_length")

    if api_name == "id":
        return "String(32)"
    if data_type in TYPE_MAP:
        mapped = TYPE_MAP[data_type]
        if mapped == "String":
            return "String(255)"
        return mapped
    if data_type in {"email", "phone", "website", "text", "picklist", "autonumber"}:
        return f"String({max_length})" if isinstance(max_length, int) and 0 < max_length <= 4000 else "String(255)"
    return "Text"


def is_field_exportable(field: dict[str, Any]) -> bool:
    api_name = field.get("api_name")
    if not api_name:
        return False
    return True


def render_model(module: dict[str, Any], rendered_class_name: str) -> str:
    api_name = module["api_name"]
    fields = [field for field in module.get("fields", []) if is_field_exportable(field)]
    used_attrs = {"metadata", "registry"}
    lines = [
        f"class {rendered_class_name}(Base):",
        f'    __tablename__ = "{api_name}"',
        f"    __zoho_module__ = {json.dumps(api_name)}",
    ]

    if not any(field.get("api_name") == "id" for field in fields):
        lines.append('    id = Column("id", String(32), primary_key=True)')

    for field in fields:
        name = field["api_name"]
        attr = attr_name(name, used_attrs)
        nullable = not field.get("system_mandatory", False)
        primary_key = name == "id"
        type_expr = column_type(field)
        options = ["primary_key=True"] if primary_key else [f"nullable={nullable}"]
        lines.append(f'    {attr} = Column("{name}", {type_expr}, {", ".join(options)})')

    if len(lines) == 3:
        lines.append("    pass")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate SQLAlchemy classes from exported Zoho CRM schema.")
    parser.add_argument("--schema", default="zoho_schema.json", help="Schema JSON from export_zoho_schema.py")
    parser.add_argument("--output", default="zoho_crm_models.py", help="Generated Python models file")
    args = parser.parse_args()

    schema_path = Path(args.schema)
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    modules = [module for module in schema.get("modules", []) if module.get("api_name") and module.get("fields")]

    rendered = [HEADER]
    used_classes: set[str] = set()
    for module in modules:
        base_class_name = class_name(module["api_name"])
        rendered_class_name = base_class_name
        suffix = 2
        while rendered_class_name in used_classes:
            rendered_class_name = f"{base_class_name}{suffix}"
            suffix += 1
        used_classes.add(rendered_class_name)
        rendered.append(render_model(module, rendered_class_name))

    output_path = Path(args.output)
    output_path.write_text("\n".join(rendered), encoding="utf-8")
    print(f"Wrote {output_path} with {len(modules)} model classes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
