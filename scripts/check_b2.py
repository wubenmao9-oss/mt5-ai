import os
current=os.getpid()
procs=[]
for line in os.popen('tasklist /V /FO CSV /FI "IMAGENAME eq python.exe" 2>nul').read().split("\n"):
    if "python.exe" not in line: continue
    parts=line.strip('"').split('","')
    if len(parts)<5: continue
    pid=parts[1]; cmd=parts[-1] if len(parts)>8 else ""
    if pid==str(current): continue
    lbl=""
    if "run.py" in cmd: lbl="RUNNER"
    elif "main.py" in cmd: lbl="ENGINE"
    elif "web.py" in cmd: lbl="WEB"
    if lbl:
        procs.append((pid,lbl,cmd[:100]))
print(f"Current PID: {current}")
print("Active processes:")
for pid,lbl,cmd in procs:
    print(f"  PID {pid:>6s} {lbl:8s} {cmd}")
runners=sum(1 for _,l,_ in procs if l=="RUNNER")
engines=sum(1 for _,l,_ in procs if l=="ENGINE")
print(f"\nRUNNERS: {runners} ENGINES: {engines}")
if runners==1 and engines==1:
    print("CLEAN")
elif runners==0 and engines==0:
    print("NONE RUNNING - restart with: .\\venv\\Scripts\\python.exe run.py")
else:
    print("MULTIPLE INSTANCES - need cleanup")
