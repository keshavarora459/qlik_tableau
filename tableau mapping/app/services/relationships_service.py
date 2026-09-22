# app/services/relationships_service.py
from app.services.cosmos_log_service import store_error


def map_cardinality_from_parsing(cardinality: str) -> str:
    """
    Maps Tableau cardinality to Power BI cardinality format.
    """
    if not cardinality:
        return "Unknown"

    mapping = {
        "one-to-many": "OneToMany",
        "1:n": "OneToMany",
        "many-to-one": "ManyToOne",
        "n:1": "ManyToOne",
        "one-to-one": "OneToOne",
        "1:1": "OneToOne",
        "many-to-many": "ManyToMany",
        "n:n": "ManyToMany"
    }

    return mapping.get(cardinality.lower(), "Unknown")


def convert_relationships(
    joins: list,
    project_id: str,
    workbook_id: str,
    run_id: str,
    auth_header: str
) -> list:
    """
    Converts Tableau relationships into dual structure:
    {
        tableau: {...},
        power_bi: {...}
    }
    """

    relationships = []

    try:
        for join in joins:
            try:
                tableau_cardinality = join.get("cardinality")
                powerbi_cardinality = map_cardinality_from_parsing(tableau_cardinality)

                for cond in join.get("join_conditions", []):
                    left = cond.get("left")
                    right = cond.get("right")

                    if not left or not right:
                        continue

                    try:
                        left_table, left_col = left.split(".", 1)
                        right_table, right_col = right.split(".", 1)
                    except ValueError:
                        continue

                    relationship_object = {
                        "tableau": {
                            "relationship_type": join.get("relationship_type"),
                            "from_table": left_table,
                            "to_table": right_table,
                            "cardinality": tableau_cardinality,
                            "cardinality_source": join.get("cardinality_source"),
                            "join_conditions": join.get("join_conditions", [])
                        },
                        "power_bi": {
                            "fromTable": left_table,
                            "fromColumn": left_col,
                            "toTable": right_table,
                            "toColumn": right_col,
                            "cardinality": powerbi_cardinality,
                            "crossFilterDirection": "Both",
                            "isActive": True
                        }
                    }

                    relationships.append(relationship_object)

            except Exception as e:
                store_error(
                    project_id=project_id,
                    workbook_id=workbook_id,
                    run_id=run_id,
                    error_msg="Failed to convert a specific join.",
                    technical_details=str(e),
                    auth_header=auth_header
                )
                continue

    except Exception as e:
        store_error(
            project_id=project_id,
            workbook_id=workbook_id,
            run_id=run_id,
            error_msg="Critical failure in convert_relationships.",
            technical_details=str(e),
            auth_header=auth_header
        )

    return relationships
