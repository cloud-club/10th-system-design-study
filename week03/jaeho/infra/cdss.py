"""CDSS(관리형 Kafka) REST 호출. ncp.py 의 키/서명 방식을 재사용."""
import base64, hashlib, hmac, json, sys, time, urllib.request, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))
import ncp
HOST = "https://clouddatastreamingservice.apigw.ntruss.com"

def call(method, uri, body=None):
    ts = str(int(time.time() * 1000))
    sig = base64.b64encode(hmac.new(ncp.SK.encode(), f"{method} {uri}\n{ts}\n{ncp.AK}".encode(), hashlib.sha256).digest()).decode()
    req = urllib.request.Request(HOST + uri, method=method, data=json.dumps(body).encode() if body is not None else None, headers={
        "x-ncp-apigw-timestamp": ts, "x-ncp-iam-access-key": ncp.AK, "x-ncp-apigw-signature-v2": sig, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, context=ncp.CTX) as r: return json.loads(r.read())
    except urllib.error.HTTPError as e: return {"httpError": e.code, "body": e.read().decode()}
