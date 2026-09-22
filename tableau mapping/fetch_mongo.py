import os
from dotenv import load_dotenv
import pymongo
import json
from bson import json_util

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "QT2F_Tableau")

def fetch_data(database_name, collection_name, limit=5):
    try:
        print(f"Connecting to MongoDB...")
        client = pymongo.MongoClient(MONGO_URI)
        
        db = client[database_name]
        collection = db[collection_name]
        
        print(f"Fetching up to {limit} documents from {database_name}.{collection_name}...")
        documents = list(collection.find().limit(limit))
        
        if not documents:
            print(f"No documents found in the collection '{collection_name}'.")
        else:
            print(f"Successfully fetched {len(documents)} document(s):")
            # Using bson.json_util to handle ObjectId and other BSON types
            print(json.dumps(documents, default=json_util.default, indent=4))
            
    except Exception as e:
        print(f"An error occurred: {e}")

if __name__ == "__main__":
    # You can change the database and collection names here
    # Available databases: 'QlikDB', 'qlik', 'QT2F_Tableau', 'sample_mflix'
    fetch_data(database_name=MONGO_DB_NAME, collection_name="mapping_results")
