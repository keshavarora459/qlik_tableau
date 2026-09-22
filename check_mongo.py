import pymongo

client = pymongo.MongoClient("mongodb+srv://krishnau2097_db_user:RNe1HMkPZj4mQdgY@qlik.91dnbo8.mongodb.net/?appName=Qlik")
db = client["QT2F_Tableau"]
collection = db["parsing_results"]

doc = collection.find_one({"workbook_id": "3d814c59-137c-4311-b60a-0f631ea66eda"})
if doc:
    payload = doc.get("payload", {})
    lm = payload.get("logical_model", {})
    if isinstance(lm, dict):
        print("Keys in logical_model:", lm.keys())
    elif isinstance(lm, list):
        print("logical_model is a list of length:", len(lm))
        if len(lm) > 0:
            print("First item keys:", lm[0].keys())
