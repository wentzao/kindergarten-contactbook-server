import pprint
from app import app

with app.test_client() as c:
    print("--- NEWS ---")
    response = c.get('/api/news/')
    print("STATUS:", response.status_code)
    try:
        pprint.pprint(response.json)
    except: pass
    
    print("\n--- SURVEYS ---")
    response = c.get('/api/survey/available/B225851150')
    print("STATUS:", response.status_code)
    try:
        pprint.pprint(response.json)
    except: pass
