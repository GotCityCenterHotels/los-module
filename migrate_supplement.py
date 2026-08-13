from services.supplement_schema_service import ensure_supplement_schema


def main():
    ensure_supplement_schema()
    print("Supplement Database A schema is ready.")


if __name__ == "__main__":
    main()
