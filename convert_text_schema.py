import argparse
import json
import re
from pathlib import Path


def infer_type(field_name: str) -> str:
    lower = field_name.lower()
    if lower == "id" or lower.endswith("_id"):
        return "text"
    if lower in {"owner", "created_by", "modified_by"} or lower.endswith("_name") and lower not in {
        "first_name",
        "last_name",
        "full_name",
        "deal_name",
        "account_name",
        "vendor_name",
        "campaign_name",
        "product_name",
        "price_book_name",
        "owner_name",
        "manager_name",
        "department_name",
        "role_name",
        "team_name",
        "department_head_name",
        "parent_department_name",
        "reports_to_role_name",
        "team_lead_name",
    }:
        return "lookup"
    if "email" in lower:
        return "email"
    if "phone" in lower or lower in {"mobile", "fax"}:
        return "phone"
    if "website" in lower or lower == "twitter" or "skype" in lower:
        return "website"
    if "time" in lower:
        return "datetime"
    if "date" in lower or lower == "closing_date":
        return "date"
    if lower.startswith("is_") or lower.endswith("_out") or lower in {"locked__s", "all_day", "recurring_activity", "active"}:
        return "boolean"
    if lower == "salary":
        return "integer"
    if any(
        token in lower
        for token in [
            "amount",
            "revenue",
            "price",
            "cost",
            "subtotal",
            "sub_total",
            "discount",
            "tax",
            "adjustment",
            "grand_total",
            "balance",
            "probability",
            "latitude",
            "longitude",
            "commission",
        ]
    ):
        return "currency" if "probability" not in lower and "latitude" not in lower and "longitude" not in lower else "double"
    if any(token in lower for token in ["employees", "duration", "number", "no_of", "qty", "quantity", "level", "num_"]):
        return "integer"
    if lower in {"description", "next_step", "check_in_comment", "terms_and_conditions", "answer", "question", "note_content"}:
        return "textarea"
    if any(token in lower for token in ["status", "stage", "source", "rating", "type", "industry", "priority", "purpose"]):
        return "picklist"
    if lower in {"tag", "participants"}:
        return "multiselectpicklist"
    return "text"


def parse_schema(text: str) -> list[dict]:
    modules = []
    current = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        match = re.fullmatch(r"===\s*(.+?)\s*===", line)
        if match:
            current = {
                "api_name": match.group(1).strip(),
                "module_metadata": {"api_name": match.group(1).strip(), "status": "visible"},
                "module_detail": None,
                "module_detail_error": None,
                "fields": [],
                "field_count": 0,
                "fields_error": None,
            }
            modules.append(current)
            continue

        if current is None:
            continue

        api_name = line
        current["fields"].append(
            {
                "api_name": api_name,
                "data_type": infer_type(api_name),
                "system_mandatory": api_name == "id" or api_name in {"Last_Name", "Company", "Deal_Name", "Account_Name"},
                "length": 255,
            }
        )

    for module in modules:
        if not any(field["api_name"] == "id" for field in module["fields"]):
            module["fields"].insert(
                0,
                {"api_name": "id", "data_type": "text", "system_mandatory": True, "length": 32},
            )
        module["field_count"] = len(module["fields"])

    return modules


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert a simple text module/field schema into zoho_schema.json format.")
    parser.add_argument("--input", default="schema", help="Text schema file")
    parser.add_argument("--output", default="zoho_schema.json", help="Output JSON schema file")
    args = parser.parse_args()

    text = Path(args.input).read_text(encoding="utf-8")
    modules = parse_schema(text)
    payload = {
        "source": args.input,
        "api_version": "local-text-schema",
        "api_domain": "local",
        "module_count": len(modules),
        "field_count": sum(module["field_count"] for module in modules),
        "failed_module_count": 0,
        "modules": modules,
    }
    Path(args.output).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {args.output}: {payload['module_count']} modules, {payload['field_count']} fields")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
