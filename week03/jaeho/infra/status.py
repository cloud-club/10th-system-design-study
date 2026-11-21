"""fcfs 리소스 상태 한눈에 보기: python status.py"""
import json, os, sys
sys.path.insert(0, os.path.dirname(__file__)); import cdss; ncp = cdss.ncp
st = json.load(open(os.path.join(os.path.dirname(__file__), 'state.json')))
r = ncp.call('getServerInstanceList', {f'serverInstanceNoList.{i}': n for i, n in enumerate(st['servers'].values(), 1)}, '/vserver/v2')
for s in r['getServerInstanceListResponse']['serverInstanceList']:
    nic = ncp.call('getNetworkInterfaceList', {'networkInterfaceNoList.1': s['networkInterfaceNoList'][0]}, '/vserver/v2')
    ip = nic['getNetworkInterfaceListResponse']['networkInterfaceList'][0]['ip']
    print(f"server {s['serverName']:14} {s['zoneCode']} {s['serverInstanceStatusName']:10} public={s.get('publicIp')} private={ip}")
m = ncp.call('getCloudMysqlInstanceDetail', {'cloudMysqlInstanceNo': st['mysql']})
d = m.get('getCloudMysqlInstanceDetailResponse', {}).get('cloudMysqlInstanceList', [{}])[0]
print('mysql', d.get('cloudMysqlInstanceStatusName'), [(x.get('privateDomain'), x.get('cloudMysqlServerInstanceStatusName')) for x in d.get('cloudMysqlServerInstanceList', [])])
c = cdss.call('POST', f"/api/v1/cluster/getClusterInfoList/{st['cdss']}")
print('kafka', (c.get('result') or {}).get('status'), c.get('httpError', ''))
if st.get('lb'):
    l = ncp.call('getLoadBalancerInstanceDetail', {'loadBalancerInstanceNo': st['lb']}, '/vloadbalancer/v2')
    x = l['getLoadBalancerInstanceDetailResponse']['loadBalancerInstanceList'][0]
    print('lb', x['loadBalancerInstanceStatusName'], x['loadBalancerDomain'])
