import requests
import json

def test():
    srv = 'microsoft'
    url = f'https://5sim.net/v1/guest/prices?product={srv}'
    print(f"Fetching {url}")
    try:
        resp = requests.get(url, timeout=10)
        print(f"Status: {resp.status_code}")
        if resp.status_code == 200:
            data = resp.json()
            # print(json.dumps(data, indent=2)[:500])
            ctry = 'hongkong'
            price = data.get(srv, {}).get(ctry, {}).get('any', {}).get('cost', 'Unknown')
            print(f"Price for {srv}/{ctry}/any: {price}")
            
            # Check if keys are actually capitalized or something
            if price == 'Unknown':
                print("Keys found in data:", list(data.keys())[:5])
                srv_data = data.get(srv, {})
                print(f"Countries found for {srv}:", list(srv_data.keys())[:10])
                ctry_data = srv_data.get(ctry, {})
                print(f"Operators found for {srv} in {ctry}:", list(ctry_data.keys())[:10])
    except Exception as e:
        print(f"Error: {e}")

test()
