
# 1️⃣ IMPORT THE LOGGERS
from app.services.cosmos_log_service import store_activity, store_error, store_log


def extract_objects_from_zones(zones):
    """
    Recursively finds all objects in the zone hierarchy:
    - Bitmaps (Images)
    - Text objects
    - Named objects (Sheets/Visuals)
    """
    objects = []

    def traverse(node):
        if not isinstance(node, dict):
            return

        attrs = node.get("attributes", {})
        # Use case-insensitive get for type-v2
        type_v2 = (attrs.get("type-v2") or "").lower()
        name = attrs.get("name")

        # Capture Bitmaps / Images
        if type_v2 in ["bitmap", "image"]:
            img_path = attrs.get("param") or attrs.get("name") or "Unknown Image"
            objects.append(f"Image ({img_path})")

        # Capture Web Pages / URLs
        elif type_v2 == "web":
            url = attrs.get("param") or "Unknown URL"
            objects.append(f"Web Page ({url})")

        # Capture Text objects
        elif type_v2 == "text":
            text_runs = node.get("formatted_text", {}).get("runs", [])
            text_content = "".join(run.get("text", "") for run in text_runs).strip()
            # Clean up to strictly ASCII characters to match parser and avoid duplicates
            text_content = "".join(c for c in text_content if ord(c) < 128).strip()
            if text_content:
                objects.append(f"Text Header ({text_content})")

        # Capture Sheets / Visuals (usually have a name but no type-v2 content like bitmap/text)
        elif name and not type_v2.startswith("layout"):
             objects.append(name)

        # Recurse into children
        for child in node.get("children", []):
            traverse(child)

    for zone in zones:
        traverse(zone)

    return objects


def extract_slicers_from_zones(zones):
    slicers = []

    def traverse(node):
        if not isinstance(node, dict):
            return

        attrs = node.get("attributes", {})
        type_v2 = attrs.get("type-v2")

        if type_v2 in ["filter", "paramctrl"]:
            param_name = attrs.get("param")

            if param_name:
                slicers.append({
                    "name": param_name,
                    "source": "parameter" if type_v2 == "paramctrl" else "field",
                    "instruction": f"Create a slicer using the {param_name}"
                })

        for child in node.get("children", []):
            traverse(child)

    for zone in zones:
        traverse(zone)

    return slicers

# 2️⃣ UPDATE SIGNATURE TO ACCEPT TRACKING IDs
def extract_dashboards_and_stories(
    payload: dict,
    project_id: str,
    workbook_id: str,
    run_id: str,
    auth_header: str
) -> dict:
    """
    Extract dashboards and stories from parsing payload.
    """

    # 3️⃣ LOG ACTIVITY: Agent starts extraction
    store_activity(
        project_id=project_id,
        workbook_id=workbook_id,
        run_id=run_id,
        technical_message="Extracting dashboards and stories from workbook payload.",
        auth_header=auth_header
    )

    try:
        ds_block = payload.get("dashboards_and_stories") or payload.get("dashboards") or {}
        if isinstance(ds_block, dict):
            dashboards = ds_block.get("dashboards", [])
            stories = ds_block.get("stories", [])
        elif isinstance(ds_block, list):
            dashboards = ds_block
            stories = payload.get("stories", [])
        else:
            dashboards = []
            stories = []

        if not stories and "stories" in payload:
            s_val = payload.get("stories")
            stories = s_val.get("stories", []) if isinstance(s_val, dict) else (s_val if isinstance(s_val, list) else [])


        for dashboard in dashboards:

            slicers = []
            # Rebuild dashboard_objects from scratch to avoid duplicates from raw parser
            dashboard_objects = []
            zone_hierarchy = dashboard.get("zone_hierarchy", {})

            for layout_name, layout in zone_hierarchy.items():

                # Extract slicers
                found_slicers = extract_slicers_from_zones(layout)
                for s in found_slicers:
                    s["layout"] = layout_name
                    slicers.append(s)

                # Extract all dashboard objects (Sheets, Text, Images)
                objects_from_zones = extract_objects_from_zones(layout)
                dashboard_objects.extend(objects_from_zones)

            dashboard["slicers"] = slicers
            # Remove duplicates while preserving order
            dashboard["dashboard_objects"] = list(dict.fromkeys(dashboard_objects))

        # 📝 STANDARD LOG: Successfully finished
        store_log(
            project_id=project_id,
            project_name="Unknown",
            workbook_id=workbook_id,
            run_id=run_id,
            function_name="extract_dashboards_and_stories",
            log_level="INFO",
            message=f"Successfully extracted {len(dashboards)} dashboards and {len(stories)} stories.",
            auth_header=auth_header
        )

        return {
            "dashboards": dashboards,
            "stories": stories
        }

    except Exception as e:
        # 🚨 LOG ERROR: Catch extraction failures
        store_error(
            project_id=project_id,
            workbook_id=workbook_id,
            run_id=run_id,
            error_msg="Failed to extract dashboards and stories.",
            technical_details=str(e),
            auth_header=auth_header
        )

        # Return safe empty defaults so the rest of the mapping process doesn't crash
        return {
            "dashboards": [],
            "stories": []
        }
