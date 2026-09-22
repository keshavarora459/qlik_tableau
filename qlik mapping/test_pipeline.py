import json
import os
import requests

def test_pipeline():
    print("==================================================================")
    print("TEST 1: Weather Analytics (Google BigQuery)")
    print("==================================================================")
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    with open(os.path.join(base_dir, "mapping2.md"), "r", encoding="utf-8") as f:
        weather_map = json.load(f)

    # Let's map connection with our updated mapping logic
    from services.connection_mapper import ConnectionMapper
    cm = ConnectionMapper()
    mapped_conns = cm.map_connections(weather_map.get("connections", []))
    weather_map["connections"] = mapped_conns

    # Call Generation Agent on port 8003
    r = requests.post("http://127.0.0.1:8003/api/generate", json={"mapping_result": weather_map, "deploy": "none"}, timeout=30)
    print("Weather generation response code:", r.status_code)
    res_weather = r.json()
    print("Weather generation status:", res_weather.get("status"))
    print("Weather generation message:", res_weather.get("message"))
    sm_weather = res_weather.get("artifacts", {}).get("semantic_model", {})
    for k in sorted(sm_weather.keys()):
        if k.startswith("definition/tables/") or k == "definition/expressions.tmdl":
            print(f"\n--- {k} ---")
            lines = sm_weather[k].strip().splitlines()
            for line in lines[:8]:
                print(f"  {line}")
            if len(lines) > 8:
                print("  ...")
                for line in lines[-6:]:
                    print(f"  {line}")

    print("\n==================================================================")
    print("TEST 2: FleetVision KSA (Amazon Redshift)")
    print("==================================================================")
    with open(os.path.join(base_dir, "MAPPINGRESPONSE.md"), "r", encoding="utf-8") as f:
        fleet_map = json.load(f)

    from agents.coordinator_agent import CoordinatorAgent
    coord = CoordinatorAgent()
    processed_tables = coord._process_tables(fleet_map.get("tables", []), fleet_map.get("connections", []))
    fleet_map["tables"] = processed_tables
    
    r = requests.post("http://127.0.0.1:8003/api/generate", json={"mapping_result": fleet_map, "deploy": "none"}, timeout=30)
    print("FleetVision generation response code:", r.status_code)
    res_fleet = r.json()
    print("FleetVision generation status:", res_fleet.get("status"))
    print("FleetVision generation message:", res_fleet.get("message"))
    sm_fleet = res_fleet.get("artifacts", {}).get("semantic_model", {})
    
    for tname in ["Trips", "Loads", "Drivers", "FuelByTrip", "Calendar"]:
        k = f"definition/tables/{tname}.tmdl"
        if k in sm_fleet:
            print(f"\n--- {k} ---")
            lines = sm_fleet[k].strip().splitlines()
            for line in lines[:5]:
                print(f"  {line}")
            print("  ...")
            for line in lines[-7:]:
                print(f"  {line}")

if __name__ == "__main__":
    test_pipeline()
