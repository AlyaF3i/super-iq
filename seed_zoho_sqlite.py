import argparse
import importlib.util
import json
import random
import string
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session


FIRST_NAMES = ["Alex", "Maya", "Omar", "Lina", "Noah", "Sara", "Adam", "Nora", "Zaid", "Layla"]
LAST_NAMES = ["Khan", "Smith", "Patel", "Garcia", "Haddad", "Brown", "Nasser", "Lee", "Wilson", "Ahmed"]
COMPANIES = ["Northstar Trading", "Blue Peak Systems", "Urban Nest", "Cedar Labs", "Gulfline Foods"]
PRODUCTS = ["CRM Starter Pack", "Analytics Dashboard", "Support Desk License", "Field Sales Mobile", "Automation Bundle"]
CITIES = ["Dubai", "Abu Dhabi", "Sharjah", "Riyadh", "Doha", "London", "New York"]
COUNTRIES = ["United Arab Emirates", "Saudi Arabia", "Qatar", "United Kingdom", "United States"]
INDUSTRIES = ["Technology", "Finance", "Healthcare", "Retail", "Manufacturing", "Education", "Logistics"]
LEAD_STATUSES = ["New", "Contacted", "Qualified", "Lost", "Converted"]
DEAL_STAGES = ["Qualification", "Needs Analysis", "Proposal", "Negotiation", "Closed Won", "Closed Lost"]
ORDER_STATUSES = ["Draft", "Confirmed", "Delivered", "Cancelled", "Invoiced"]
CASE_STATUSES = ["New", "Escalated", "In Progress", "On Hold", "Closed"]
DEPARTMENTS = ["Sales", "Marketing", "Customer Success", "Support", "Finance", "Operations"]
ROLES = ["Sales Rep", "Account Executive", "Sales Manager", "Marketing Manager", "Support Agent", "Operations Lead"]
SALARY_RANGES = {
    "Sales Rep": (9000, 16000),
    "Account Executive": (14000, 24000),
    "Sales Manager": (24000, 38000),
    "Marketing Manager": (22000, 34000),
    "Support Agent": (7000, 14000),
    "Operations Lead": (18000, 30000),
}
TEAMS = ["Enterprise", "SMB", "Inbound", "Renewals", "Support Tier 1", "Partner Channel"]
EMPLOYEE_POOL = [
    {
        "id": str(1263695000001000000 + index),
        "Employee_Number": f"EMP-{index + 1:04d}",
        "First_Name": first,
        "Last_Name": last,
        "Full_Name": f"{first} {last}",
        "Email": f"{first.lower()}.{last.lower()}@example.com",
        "Phone": f"+9715{random.randint(10000000, 99999999)}",
        "Mobile": f"+9715{random.randint(10000000, 99999999)}",
        "Department_Id": str(1263695000002000000 + (index % len(DEPARTMENTS))),
        "Department_Name": DEPARTMENTS[index % len(DEPARTMENTS)],
        "Role_Id": str(1263695000003000000 + (index % len(ROLES))),
        "Role_Name": ROLES[index % len(ROLES)],
        "Manager_Id": str(1263695000001000000 + (index % 5)),
        "Manager_Name": "",
        "Team_Id": str(1263695000004000000 + (index % len(TEAMS))),
        "Team_Name": TEAMS[index % len(TEAMS)],
        "Status": "Active" if index % 11 else "Inactive",
        "Hire_Date": date.today() - timedelta(days=120 + index * 17),
        "Location": CITIES[index % len(CITIES)],
        "Employment_Type": "Full Time" if index % 7 else "Contract",
    }
    for index, (first, last) in enumerate((f, l) for f in FIRST_NAMES for l in LAST_NAMES)
]
for employee in EMPLOYEE_POOL:
    manager = EMPLOYEE_POOL[int(employee["Manager_Id"]) - 1263695000001000000]
    employee["Manager_Name"] = manager["Full_Name"]


def load_models(path: Path):
    spec = importlib.util.spec_from_file_location("zoho_crm_models", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load generated models from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def rand_id() -> str:
    return str(random.randint(10**17, 10**18 - 1))


def employee_for_index(index: int | None = None) -> dict[str, Any]:
    if index is None:
        return random.choice(EMPLOYEE_POOL)
    return EMPLOYEE_POOL[index % len(EMPLOYEE_POOL)]


def employee_lookup(employee: dict[str, Any]) -> dict[str, str]:
    return {"id": employee["id"], "name": employee["Full_Name"]}


def salary_for_role(role_name: str) -> int:
    low, high = SALARY_RANGES.get(role_name, (8000, 20000))
    return round(random.randint(low, high) / 100) * 100


def picklist_value(field: dict[str, Any]) -> Any:
    values = [
        item.get("actual_value") or item.get("display_value")
        for item in field.get("pick_list_values", [])
        if item.get("actual_value") or item.get("display_value")
    ]
    return random.choice(values) if values else None


def sample_value(module_samples: list[dict[str, Any]], api_name: str) -> Any:
    values = [row.get(api_name) for row in module_samples if row.get(api_name) not in {None, ""}]
    if not values:
        return None
    value = random.choice(values)
    if isinstance(value, str):
        if "@" in value:
            name = "".join(random.choice(string.ascii_lowercase) for _ in range(7))
            return f"{name}@example.com"
        if value.replace(".", "", 1).isdigit():
            return value
        if len(value) > 3 and not value.isupper():
            return f"{value.split()[0]} {random.choice(LAST_NAMES)}"[:255]
    return value


def fake_by_name(api_name: str) -> Any:
    lower = api_name.lower()
    if lower in {"owner", "created_by", "modified_by", "manager", "department_head", "team_lead"}:
        return employee_lookup(employee_for_index())
    if lower == "id" or lower.endswith("_id"):
        return rand_id()
    if lower in {"owner_name", "created_by_name", "modified_by_name", "manager_name", "department_head_name", "team_lead_name"}:
        return employee_for_index()["Full_Name"]
    if lower in {"owner_id", "created_by_id", "modified_by_id", "manager_id", "department_head_id", "team_lead_id"}:
        return employee_for_index()["id"]
    if "department_name" in lower:
        return random.choice(DEPARTMENTS)
    if "role_name" in lower:
        return random.choice(ROLES)
    if "team_name" in lower:
        return random.choice(TEAMS)
    if "department_code" in lower:
        return f"DEP-{random.randint(100, 999)}"
    if "role_code" in lower:
        return f"ROLE-{random.randint(100, 999)}"
    if "team_code" in lower:
        return f"TEAM-{random.randint(100, 999)}"
    if "employee_number" in lower:
        return f"EMP-{random.randint(1000, 9999)}"
    if "first_name" in lower:
        return random.choice(FIRST_NAMES)
    if "last_name" in lower:
        return random.choice(LAST_NAMES)
    if "full_name" in lower or lower == "name":
        return f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}"
    if "company" in lower or "account_name" in lower or "vendor_name" in lower:
        return random.choice(COMPANIES)
    if "product_name" in lower:
        return random.choice(PRODUCTS)
    if "campaign_name" in lower:
        return random.choice(["Q1 Renewal Push", "Enterprise Webinar", "Dubai Trade Show", "Partner Referral", "Product Launch"])
    if lower in {"subject", "note_title", "solution_title"}:
        return random.choice(["Follow-up required", "Pricing review", "Implementation question", "Renewal discussion", "Support escalation"])
    if "product_code" in lower:
        return f"SKU-{random.randint(1000, 9999)}"
    if "tracking_number" in lower or "case_number" in lower or "account_number" in lower:
        return str(random.randint(100000, 999999))
    if "email" in lower:
        name = "".join(random.choice(string.ascii_lowercase) for _ in range(7))
        return f"{name}@example.com"
    if "phone" in lower or "mobile" in lower:
        return f"+9715{random.randint(10000000, 99999999)}"
    if "city" in lower:
        return random.choice(CITIES)
    if "country" in lower:
        return random.choice(COUNTRIES)
    if "order_status" in lower or lower in {"po_status", "invoice_status"}:
        return random.choice(ORDER_STATUSES)
    if "case" in lower and "status" in lower:
        return random.choice(CASE_STATUSES)
    if lower == "industry" or lower.endswith("_industry"):
        return random.choice(INDUSTRIES)
    if "status" in lower:
        return random.choice(LEAD_STATUSES)
    if "stage" in lower:
        return random.choice(DEAL_STAGES)
    if any(token in lower for token in ["amount", "revenue", "price", "cost", "total", "discount", "tax", "balance", "adjustment"]):
        return round(random.uniform(250, 75000), 2)
    if "probability" in lower or "percent" in lower:
        return random.randint(1, 100)
    if any(token in lower for token in ["qty", "quantity", "reorder", "num_sent"]):
        return random.randint(1, 500)
    if "category" in lower:
        return random.choice(["Software", "Services", "Hardware", "Training", "Support"])
    if "carrier" in lower:
        return random.choice(["DHL", "FedEx", "Aramex", "UPS", "Local Courier"])
    if "priority" in lower:
        return random.choice(["Low", "Normal", "High", "Urgent"])
    if "origin" in lower:
        return random.choice(["Email", "Phone", "Web", "Portal", "Chat"])
    if "description" in lower:
        return "Generated local test record for Zoho CRM mirror."
    if "question" in lower:
        return "How should this customer issue be handled?"
    if "answer" in lower:
        return "Review the account context, confirm scope, and follow the standard resolution workflow."
    if "note_content" in lower or "terms" in lower:
        return "Synthetic CRM note for local analytics testing."
    return None


def fake_value(field: dict[str, Any], module_samples: list[dict[str, Any]] | None = None) -> Any:
    api_name = field.get("api_name", "field")
    data_type = str(field.get("data_type") or field.get("json_type") or "").lower().replace(" ", "")

    if module_samples:
        sampled = sample_value(module_samples, api_name)
        if sampled is not None:
            return sampled

    named = fake_by_name(api_name)
    if named is not None:
        return named

    if data_type in {"picklist", "multiselectpicklist"}:
        value = picklist_value(field)
        if data_type == "multiselectpicklist":
            return [value] if value else []
        return value or random.choice(["New", "Active", "Inactive"])
    if data_type in {"boolean"}:
        return random.choice([True, False])
    if data_type in {"integer", "bigint", "long"}:
        return random.randint(1, 1000)
    if data_type in {"double", "currency", "percent"}:
        return round(random.uniform(10, 10000), 2)
    if data_type == "date":
        return date.today() - timedelta(days=random.randint(0, 1200))
    if data_type == "datetime":
        return datetime.now(timezone.utc) - timedelta(days=random.randint(0, 1200))
    if data_type in {"lookup", "ownerlookup", "multiselectlookup", "multiuserlookup"}:
        return {"id": rand_id(), "name": f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}"}
    if data_type in {"subform", "fileupload", "imageupload", "jsonobject", "jsonarray"}:
        return []

    length = field.get("length") if isinstance(field.get("length"), int) else 80
    return f"Sample {api_name}".strip()[: max(1, min(length, 255))]


def module_class_map(models_module) -> dict[str, type]:
    return {
        value.__zoho_module__: value
        for value in vars(models_module).values()
        if isinstance(value, type) and hasattr(value, "__zoho_module__")
    }


def employee_module_row(index: int) -> dict[str, Any]:
    template = employee_for_index(index)
    first = FIRST_NAMES[index % len(FIRST_NAMES)]
    last = LAST_NAMES[(index // len(FIRST_NAMES)) % len(LAST_NAMES)]
    department_index = index % len(DEPARTMENTS)
    role_index = index % len(ROLES)
    team_index = index % len(TEAMS)
    employee = {
        **template,
        "id": str(1263695000001000000 + index),
        "Employee_Number": f"EMP-{index + 1:04d}",
        "First_Name": first,
        "Last_Name": last,
        "Full_Name": f"{first} {last}",
        "Email": f"{first.lower()}.{last.lower()}.{index + 1:04d}@example.com",
        "Department_Id": str(1263695000002000000 + department_index),
        "Department_Name": DEPARTMENTS[department_index],
        "Role_Id": str(1263695000003000000 + role_index),
        "Role_Name": ROLES[role_index],
        "Salary": salary_for_role(ROLES[role_index]),
        "Manager_Id": str(1263695000001000000 + (index % 5)),
        "Team_Id": str(1263695000004000000 + team_index),
        "Team_Name": TEAMS[team_index],
        "Hire_Date": date.today() - timedelta(days=120 + index * 17),
        "Location": CITIES[index % len(CITIES)],
        "Employment_Type": "Full Time" if index % 7 else "Contract",
        "Created_Time": datetime.now(timezone.utc) - timedelta(days=600 + index),
        "Modified_Time": datetime.now(timezone.utc) - timedelta(days=index % 90),
    }
    employee["Manager_Name"] = employee_for_index(index % 5)["Full_Name"]
    employee["Tag"] = ["employee", employee["Department_Name"]]
    return employee


def department_module_row(index: int) -> dict[str, Any]:
    name = DEPARTMENTS[index % len(DEPARTMENTS)]
    head = employee_for_index(index)
    return {
        "id": str(1263695000002000000 + index),
        "Department_Name": name,
        "Department_Code": f"DEP-{index + 1:03d}",
        "Department_Head_Id": head["id"],
        "Department_Head_Name": head["Full_Name"],
        "Parent_Department_Id": "" if index % len(DEPARTMENTS) == 0 else str(1263695000002000000),
        "Parent_Department_Name": "" if index % len(DEPARTMENTS) == 0 else DEPARTMENTS[0],
        "Location": CITIES[index % len(CITIES)],
        "Status": "Active",
        "Created_Time": datetime.now(timezone.utc) - timedelta(days=900 + index),
        "Modified_Time": datetime.now(timezone.utc) - timedelta(days=index % 60),
        "Description": f"{name} department for local CRM analytics testing.",
        "Tag": ["department"],
    }


def role_module_row(index: int) -> dict[str, Any]:
    name = ROLES[index % len(ROLES)]
    return {
        "id": str(1263695000003000000 + index),
        "Role_Name": name,
        "Role_Code": f"ROLE-{index + 1:03d}",
        "Reports_To_Role_Id": "" if index % len(ROLES) == 0 else str(1263695000003000000),
        "Reports_To_Role_Name": "" if index % len(ROLES) == 0 else ROLES[0],
        "Level": index % 5 + 1,
        "Status": "Active",
        "Created_Time": datetime.now(timezone.utc) - timedelta(days=900 + index),
        "Modified_Time": datetime.now(timezone.utc) - timedelta(days=index % 60),
        "Description": f"{name} role for local CRM analytics testing.",
        "Tag": ["role"],
    }


def team_module_row(index: int) -> dict[str, Any]:
    name = TEAMS[index % len(TEAMS)]
    lead = employee_for_index(index)
    department_index = index % len(DEPARTMENTS)
    return {
        "id": str(1263695000004000000 + index),
        "Team_Name": name,
        "Team_Code": f"TEAM-{index + 1:03d}",
        "Department_Id": str(1263695000002000000 + department_index),
        "Department_Name": DEPARTMENTS[department_index],
        "Team_Lead_Id": lead["id"],
        "Team_Lead_Name": lead["Full_Name"],
        "Region": random.choice(["UAE", "GCC", "EMEA", "North America"]),
        "Status": "Active",
        "Created_Time": datetime.now(timezone.utc) - timedelta(days=800 + index),
        "Modified_Time": datetime.now(timezone.utc) - timedelta(days=index % 45),
        "Description": f"{name} team for local CRM analytics testing.",
        "Tag": ["team"],
    }


def special_module_row(api_name: str, index: int) -> dict[str, Any] | None:
    if api_name == "Employees":
        return employee_module_row(index)
    if api_name == "Departments":
        return department_module_row(index)
    if api_name == "Roles":
        return role_module_row(index)
    if api_name == "Teams":
        return team_module_row(index)
    return None


def row_for_module(module_schema: dict[str, Any], model_class: type, samples: dict[str, list[dict[str, Any]]], index: int) -> dict[str, Any]:
    mapper = inspect(model_class)
    column_to_attr = {
        column.name: prop.key
        for prop in mapper.column_attrs
        for column in prop.columns
    }
    api_name = module_schema.get("api_name") or ""
    special_row = special_module_row(api_name, index) or {}
    row = {column_to_attr[key]: value for key, value in special_row.items() if key in column_to_attr}
    module_samples = samples.get(api_name, [])
    owner = employee_for_index(index)
    for field in module_schema.get("fields", []):
        field_name = field.get("api_name")
        attr = column_to_attr.get(field_name)
        if not attr or attr in row:
            continue
        if field_name == "Owner":
            row[attr] = employee_lookup(owner)
        elif field_name == "Owner_Id":
            row[attr] = owner["id"]
        elif field_name == "Owner_Name":
            row[attr] = owner["Full_Name"]
        elif field_name in {"Created_By", "Modified_By"}:
            row[attr] = employee_lookup(employee_for_index(index + 1))
        else:
            row[attr] = fake_value(field, module_samples)
    row.setdefault("id", rand_id())
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description="Create and seed a local SQLite database from Zoho CRM models.")
    parser.add_argument("--schema", default="zoho_schema.json", help="Schema JSON from export_zoho_schema.py")
    parser.add_argument("--models", default="zoho_crm_models.py", help="Generated models file")
    parser.add_argument("--database", default="zoho_crm_local.sqlite3", help="SQLite database path")
    parser.add_argument("--samples", default=None, help="Optional zoho_samples.json from export_zoho_samples.py")
    parser.add_argument("--rows", type=int, default=25, help="Rows to generate per module. Default: 25")
    parser.add_argument("--drop-existing", action="store_true", help="Drop existing local tables before seeding")
    args = parser.parse_args()

    schema = json.loads(Path(args.schema).read_text(encoding="utf-8"))
    samples = {}
    if args.samples and Path(args.samples).exists():
        sample_payload = json.loads(Path(args.samples).read_text(encoding="utf-8"))
        samples = {
            api_name: module.get("records", [])
            for api_name, module in sample_payload.get("modules", {}).items()
        }
    models_module = load_models(Path(args.models))
    classes = module_class_map(models_module)
    engine = create_engine(f"sqlite:///{Path(args.database).resolve()}")

    if args.drop_existing:
        models_module.Base.metadata.drop_all(engine)
    models_module.Base.metadata.create_all(engine)

    inserted = 0
    with Session(engine) as session:
        for module_schema in schema.get("modules", []):
            api_name = module_schema.get("api_name")
            model_class = classes.get(api_name)
            if not model_class:
                continue
            for index in range(args.rows):
                session.add(model_class(**row_for_module(module_schema, model_class, samples, index)))
                inserted += 1
        session.commit()

    print(f"Wrote {inserted} rows to {args.database}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
