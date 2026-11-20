"""Local E2. Run from the CSC3104 repo. All outputs go beside this script."""
import argparse
import csv
import http.cookiejar
import json
import random
import re
import secrets
import statistics
import subprocess
import time
import uuid
from pathlib import Path
from urllib import parse, request

ROOT = Path(__file__).resolve().parent
URL = "http://127.0.0.1:8000"
KUBE = ["docker","compose","exec","-T","k3-server","kubectl"]
NS = "idlecloud-jobs"
TAINT = "idlecloud/owner-active=true:NoExecute"
SELECTOR = "app.kubernetes.io/managed-by=idlecloud-coordinator"
TERMINAL = {"COMPLETED","FAILED","CANCELLED"}
FIELDS = ("trial job_id requested_delay_s old_node new_node old_pod new_pod t_submit t_old_running "
          "t_taint t_taint_ack t_old_stopped t_new_running t_first_resumed_epoch t_done final_status "
          "epoch_at_taint resumed_at epochs_repeated successful release_s resume_s duration_s "
          "overhead_s pod_minus_host_clock_s clock_uncertainty_s").split()

def kub(*args):
    return subprocess.check_output(KUBE+list(args),text=True,timeout=15)

def pods(selector):
    return json.loads(kub("get","pods","-n",NS,"-l",selector,"-o","json"))["items"]

def login():
    client = request.build_opener(request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    account = "e2-"+uuid.uuid4().hex+"@example.test"
    pw = secrets.token_urlsafe(18)
    res=client.open(URL+"/signup",parse.urlencode(dict(email=account,password=pw,confirm_password=pw)).encode(),timeout=20)
    if "/signup" in res.geturl(): raise RuntimeError("Local account registration failed")
    return client

def submit(client):
    boundary=uuid.uuid4().hex
    parts=[]
    for key,val in [("job_type","pytorch"),("command","python train_ckpt.py")]:
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{val}\r\n'.encode())
    parts.extend([f'--{boundary}\r\nContent-Disposition: form-data; name="inputs"; filename="train_ckpt.py"\r\nContent-Type: text/plain\r\n\r\n'.encode(),
                  (ROOT/"train_ckpt.py").read_bytes(),f'\r\n--{boundary}--\r\n'.encode()])
    req=request.Request(URL+"/jobs/new",b"".join(parts),{"Content-Type":"multipart/form-data; boundary="+boundary})
    result=client.open(req,timeout=30)
    return str(uuid.UUID(result.geturl().rsplit("/",1)[1]))

def logs(pod,path):
    stream=path.open("wb")
    try:
        return subprocess.Popen(KUBE+["logs","-n",NS,"-f",pod],stdout=stream,stderr=subprocess.STDOUT)
    finally: stream.close()

def read_epochs(path):
    s=path.read_text(encoding="utf-8",errors="replace")
    return [(int(a),float(b)) for a,b in re.findall(r"EPOCH (\d+) START ([\d.]+)",s)],re.findall(r"RESUMED_AT (\d+)",s)

def sample_clock():
    # All local workers share the Docker VM clock. Map epoch log times onto the
    # Windows wall clock using the minimum round-trip sample before the trial.
    samples=[]
    for _ in range(4):
        a=time.time()
        stamp=float(subprocess.check_output(["docker","compose","exec","-T","coordinator","python","-c","import time;print(time.time())"],text=True))
        b=time.time()
        samples.append(((b-a)/2,stamp-(a+b)/2))
    return min(samples)

def run_trial(client,out,number,delay):
    row=dict.fromkeys(FIELDS)
    row.update(trial=number,requested_delay_s=delay)
    uncertainty,offset=sample_clock()
    row.update(clock_uncertainty_s=uncertainty,pod_minus_host_clock_s=offset)
    row['t_submit']=time.time()
    job=row['job_id']=submit(client)
    print(number,job,"submitted",flush=True)
    sel=SELECTOR+",idlecloud/job-id="+job
    processes=[]; owned=None; next_status=0; last_status=None
    oldlog=out/(job+"-old.log"); newlog=out/(job+"-new.log")
    try:
        while time.time()-row['t_submit'] < 600:
            tick=time.monotonic()
            tracking=not row['old_pod'] or (delay is not None and not (row['t_new_running'] and row['t_old_stopped']))
            if tracking:
                items=pods(sel); seen=time.time()
                running=[p for p in items if p['status']['phase']=='Running' and not p['metadata'].get('deletionTimestamp')]
                if not row['old_pod'] and running:
                    p=running[0]; row.update(old_node=p['spec']['nodeName'],old_pod=p['metadata']['name'],t_old_running=seen)
                    processes.append(logs(row['old_pod'],oldlog))
                if delay is not None and row['old_pod'] and row['t_taint'] is None and seen>=row['t_old_running']+delay:
                    node=row['old_node']
                    if node not in ('local-pc-1','local-pc-2','local-pc-3'): raise RuntimeError("Unexpected node")
                    foreign=[p for p in pods(SELECTOR) if p['spec'].get('nodeName')==node and p['status']['phase'] not in ('Succeeded','Failed') and p['metadata']['labels'].get('idlecloud/job-id')!=job]
                    if foreign: raise RuntimeError("Another task is using the target node")
                    taints=json.loads(kub('get','node',node,'-o','json'))['spec'].get('taints',[])
                    if any(t['key']=='idlecloud/owner-active' for t in taints): raise RuntimeError("Owner taint already present")
                    row['t_taint']=time.time()
                    kub('taint','node',node,TAINT); owned=node;row['t_taint_ack']=time.time()
                    continue
                if row['t_taint']:
                    old=next((p for p in items if p['metadata']['name']==row['old_pod']),None)
                    if not row['t_old_stopped'] and (not old or old['status']['phase'] in ('Succeeded','Failed') or any('terminated' in c.get('state',{}) for c in old['status'].get('containerStatuses',[]))):
                        row['t_old_stopped']=seen
                    for p in running:
                        if p['metadata']['name']!=row['old_pod'] and not row['new_pod']:
                            row.update(new_node=p['spec']['nodeName'],new_pod=p['metadata']['name'],t_new_running=seen)
                            processes.append(logs(row['new_pod'],newlog))
                            if owned:
                                kub('taint','node',owned,'idlecloud/owner-active:NoExecute-');owned=None
            if tick>=next_status:
                with client.open(URL+"/jobs/"+job+"/status",timeout=12) as res:
                    last_status=json.load(res)['status']
                next_status=tick+1
                if last_status in TERMINAL: break
            time.sleep(max(0,(.2 if tracking else 1)-(time.monotonic()-tick)))
        row.update(final_status=last_status if last_status in TERMINAL else 'TIMEOUT',t_done=time.time())
    finally:
        if owned: kub('taint','node',owned,'idlecloud/owner-active:NoExecute-')
        for proc in processes:
            try: proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.terminate();proc.wait()
        if last_status not in TERMINAL: client.open(URL+"/jobs/"+job+"/cancel",b"",timeout=15).close()
    row['duration_s']=row['t_done']-row['t_submit']
    if delay is not None:
        if row['t_taint'] and oldlog.exists():
            epochs,_=read_epochs(oldlog)
            eligible=[n for n,t in epochs if t-offset<=row['t_taint']]
            if eligible:row['epoch_at_taint']=max(eligible)
        if newlog.exists():
            epochs,resumed=read_epochs(newlog)
            if resumed: row['resumed_at']=int(resumed[0])
            if epochs: row['t_first_resumed_epoch']=epochs[0][1]-offset
        if row['epoch_at_taint'] is not None and row['resumed_at'] is not None:
            row['epochs_repeated']=row['epoch_at_taint']-row['resumed_at']
        row['successful']=int(last_status=='COMPLETED' and (row['resumed_at'] or 0)>0 and row['old_node']!=row['new_node'] and row['t_first_resumed_epoch'] is not None)
        for key,end in [('release_s','t_old_stopped'),('resume_s','t_first_resumed_epoch')]:
            if row[end] and row['t_taint']:row[key]=row[end]-row['t_taint']
    print(number,row['final_status'],row['successful'],row['resume_s'],flush=True)
    return row

def write(path,rows):
    tmp=path.with_suffix('.tmp')
    with tmp.open('w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=FIELDS);w.writeheader();w.writerows(rows)
    tmp.replace(path)

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--trials',type=int,default=10);p.add_argument('--baselines',type=int,default=3)
    p.add_argument('--seed',type=int,default=17);p.add_argument('--dry-run',action='store_true')
    p.add_argument('--out',type=Path,default=ROOT/'results')
    a=p.parse_args()
    if a.dry_run:
        print('POST /signup; POST /jobs/new (pytorch, python train_ckpt.py); GET /jobs/{id}/status')
        for action in ['get pods -n '+NS+' -l '+SELECTOR,'logs -n '+NS+' -f POD','taint node NODE '+TAINT,'taint node NODE idlecloud/owner-active:NoExecute-']:print(*KUBE,action)
        return
    a.out.mkdir(exist_ok=True,parents=True)
    rp=a.out/'recovery_trials.csv';bp=a.out/'recovery_baseline.csv'
    if rp.exists() or bp.exists():raise SystemExit('Choose a new output directory.')
    client=login();rng=random.Random(a.seed);trials=[];base=[]
    for i in range(a.trials):
        row=run_trial(client,a.out,i+1,rng.uniform(25,75));trials.append(row);write(rp,trials)
        if i==0 and not row['successful']:raise SystemExit('Pilot did not recover. Inspect logs.')
    for i in range(a.baselines):
        row=run_trial(client,a.out,i+1,None);base.append(row);write(bp,base)
    baseline=[r['duration_s'] for r in base if r['final_status']=='COMPLETED']
    if baseline:
        for row in trials:
            if row['successful']:row['overhead_s']=row['duration_s']-statistics.median(baseline)
        write(rp,trials)
    success=[r for r in trials if r['successful']]
    print('successful',len(success),'/',len(trials))
    for k in ['release_s','resume_s','epochs_repeated','overhead_s']:
        v=[r[k] for r in success if r[k] is not None]
        if len(v)>=2:
            q1,_,q3=statistics.quantiles(v,n=4,method='inclusive')
            print(k,'median',statistics.median(v),'Q1',q1,'Q3',q3,'IQR',q3-q1)
if __name__=='__main__':main()

