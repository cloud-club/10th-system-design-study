"""fcfs-poc 리소스 전부 삭제 (state.json 에 기록된 것만). 실행: python destroy.py --yes
순서: LB → 대상 그룹 → 서버(정지→반납) → MySQL → Kafka → (전부 사라진 뒤) 서브넷·ACG·초기화 스크립트·로그인 키
의존 리소스가 남아 있으면 해당 단계는 실패하니, 잠시 후 다시 실행하면 이어서 지운다."""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(__file__)); import cdss; ncp = cdss.ncp
if '--yes' not in sys.argv:
    sys.exit('정말 지우려면 --yes')
P = os.path.join(os.path.dirname(__file__), 'state.json'); st = json.load(open(P))
def say(what, r): print(what, 'ERR ' + json.dumps(r, ensure_ascii=False)[:200] if ('httpError' in r or 'responseError' in r) else 'ok')

if st.get('lb'):
    say('lb', ncp.call('deleteLoadBalancerInstances', {'loadBalancerInstanceNoList.1': st['lb']}, '/vloadbalancer/v2'))
    time.sleep(60)
if st.get('tg'):
    say('target group', ncp.call('deleteTargetGroups', {'targetGroupNoList.1': st['tg']}, '/vloadbalancer/v2'))
srv = list(st.get('servers', {}).values())
if srv:
    say('stop servers', ncp.call('stopServerInstances', {f'serverInstanceNoList.{i}': n for i, n in enumerate(srv, 1)}, '/vserver/v2'))
    for _ in range(30):
        r = ncp.call('getServerInstanceList', {f'serverInstanceNoList.{i}': n for i, n in enumerate(srv, 1)}, '/vserver/v2')
        if all(s['serverInstanceStatusName'] == 'stopped' for s in r['getServerInstanceListResponse']['serverInstanceList']): break
        time.sleep(10)
    say('terminate servers', ncp.call('terminateServerInstances', {f'serverInstanceNoList.{i}': n for i, n in enumerate(srv, 1)}, '/vserver/v2'))
if st.get('mysql'):
    say('mysql', ncp.call('deleteCloudMysqlInstance', {'cloudMysqlInstanceNo': st['mysql']}))
if st.get('cdss'):
    say('kafka', cdss.call('DELETE', f"/api/v1/cluster/deleteCDSSCluster/{st['cdss']}"))
print('서버/DB/Kafka 반납에 수 분 걸립니다. 완료 후 아래 네트워크 정리가 성공할 때까지 다시 실행하세요.')
for k in ('fcfs-pub-kr1', 'fcfs-pub-kr2', 'fcfs-priv-kr1'):
    say(f'subnet {k}', ncp.call('deleteSubnet', {'subnetNo': st[k]}, '/vpc/v2'))
say('acg', ncp.call('deleteAccessControlGroup', {'vpcNo': '131275', 'accessControlGroupNo': st['acg']}, '/vserver/v2'))
say('init script', ncp.call('deleteInitScripts', {'initScriptNoList.1': st['initScript']}, '/vserver/v2'))
say('login key', ncp.call('deleteLoginKeys', {'keyNameList.1': 'fcfs-poc-key'}, '/vserver/v2'))
if st.get('cdssConfigGroup'):
    say('kafka config group', cdss.call('POST', '/api/v1/configGroup/deleteConfigGroup', {'configGroupNo': st['cdssConfigGroup'], 'kafkaVersionCode': 3903006}))
