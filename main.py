import csv, json, os, sqlite3, tempfile, uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import polars as pl

BASE=Path(__file__).parent
DB=Path(os.getenv('DB_PATH',str(BASE/'crt_v4.db')));DB.parent.mkdir(parents=True,exist_ok=True)
app=FastAPI(title='TAM CRt Analytics V4 Hybrid',version='4.0.0')
app.mount('/static',StaticFiles(directory=BASE/'static'),name='static')
SESSIONS={}; LOCK=RLock(); OUTLET_CACHE={'code':{},'name':{}}

def conn():
 c=sqlite3.connect(DB,timeout=60);c.row_factory=sqlite3.Row;c.execute('PRAGMA journal_mode=WAL');return c
def now():return datetime.now(timezone.utc).isoformat()
def norm(x):return ' '.join(str(x or '').strip().upper().split())
def nvin(x):return ''.join(str(x or '').strip().upper().split())
with conn() as c:c.executescript('''
CREATE TABLE IF NOT EXISTS outlets(outlet_key TEXT PRIMARY KEY,outlet_name TEXT,dealer TEXT,crt_area TEXT,region TEXT,province TEXT,version TEXT,updated_at TEXT);
CREATE TABLE IF NOT EXISTS results(id TEXT PRIMARY KEY,name TEXT,crt_year INT,period TEXT,scope_level TEXT,scope_value TEXT,created_at TEXT,payload TEXT);
''')
def load_outlets():
 with conn() as c:
  rows=[dict(x) for x in c.execute('SELECT * FROM outlets')]
 OUTLET_CACHE['code']={norm(x['outlet_key']):x for x in rows};OUTLET_CACHE['name']={norm(x['outlet_name']):x for x in rows}
load_outlets()
async def spool(f):
 h=tempfile.NamedTemporaryFile(delete=False,suffix=Path(f.filename or '').suffix)
 try:
  while b:=await f.read(1024*1024):h.write(b)
  return h.name
 finally:h.close()
def open_rows(path,name):
 ext=Path(name).suffix.lower()
 if ext=='.csv':
  h=open(path,encoding='utf-8-sig',errors='replace',newline='');return h,csv.DictReader(h)
 if ext in {'.xlsx','.xls','.xlsb'}:
  d=pl.read_excel(path,engine='calamine');return d,d.iter_rows(named=True)
 raise ValueError('Supported: CSV, XLSX, XLS, XLSB')
def header(path,name):
 h,r=open_rows(path,name)
 try:return (r.fieldnames or []) if isinstance(r,csv.DictReader) else h.columns
 finally:
  if hasattr(h,'close'):h.close()
def year(x):
 if hasattr(x,'year'):return int(x.year)
 s=str(x or '')
 for i in range(max(0,len(s)-3)):
  z=s[i:i+4]
  if z.isdigit() and 1900<=int(z)<=2100:return int(z)
 try:
  n=float(x)
  if 1900<=n<=2100:return int(n)
  if 20000<=n<=80000:
   import datetime as dt
   return (dt.datetime(1899,12,30)+dt.timedelta(days=n)).year
 except:pass
 return None
def lookup(code,name):
 return OUTLET_CACHE['code'].get(norm(code)) or OUTLET_CACHE['name'].get(norm(name))
def session(sid):
 s=SESSIONS.get(sid)
 if not s:raise HTTPException(404,'Working session not found. A redeploy clears unfinished sessions.')
 return s
def eligible(s):
 lvl,val=s['scope_level'],s['scope_value']
 if lvl=='ALL':return s['vins']
 key={'OUTLET':'sales_outlet','DEALER':'sales_dealer','AREA':'sales_area'}[lvl]
 return {v:r for v,r in s['vins'].items() if r[key]==val}
@app.get('/')
def home():return FileResponse(BASE/'static'/'index.html')
@app.get('/health')
def health():return {'status':'ok','version':'4.0.0','database':str(DB),'working_sessions':len(SESSIONS)}
@app.post('/api/inspect')
async def inspect(file:UploadFile=File(...)):
 p=await spool(file)
 try:return {'columns':header(p,file.filename or '')}
 finally:Path(p).unlink(missing_ok=True)
@app.post('/api/outlets')
async def outlet_upload(file:UploadFile=File(...),outlet_name:str=Form(...),dealer:str=Form(...),crt_area:str=Form(...),outlet_code:str=Form(''),region:str=Form(''),province:str=Form(''),version:str=Form('Current')):
 p=await spool(file);saved=invalid=0
 try:
  h,rs=open_rows(p,file.filename or '')
  try:
   with conn() as c:
    for r in rs:
     name=norm(r.get(outlet_name));key=norm(r.get(outlet_code)) if outlet_code else name;d=norm(r.get(dealer));a=norm(r.get(crt_area))
     if not all([key,name,d,a]):invalid+=1;continue
     c.execute('INSERT OR REPLACE INTO outlets VALUES(?,?,?,?,?,?,?,?)',(key,name,d,a,norm(r.get(region)) if region else '',norm(r.get(province)) if province else '',version,now()));saved+=1
  finally:
   if hasattr(h,'close'):h.close()
  load_outlets();return {'saved':saved,'invalid':invalid}
 finally:Path(p).unlink(missing_ok=True)
@app.get('/api/outlets')
def outlet_status():
 with conn() as c:return dict(c.execute('SELECT COUNT(*) count,MAX(version) version,MAX(updated_at) updated_at FROM outlets').fetchone())
@app.delete('/api/outlets')
def outlet_clear():
 with conn() as c:c.execute('DELETE FROM outlets')
 load_outlets();return {'ok':1}
@app.post('/api/sessions')
def create_session(name:str=Form(...),crt_year:int=Form(...),period:str=Form(''),notes:str=Form('')):
 sid=uuid.uuid4().hex
 with LOCK:SESSIONS[sid]={'id':sid,'name':name,'crt_year':crt_year,'period':period,'notes':notes,'vins':{},'scope_level':'ALL','scope_value':'','master_audit':[],'achievement_audit':[],'created_at':now()}
 return {'id':sid}
@app.post('/api/sessions/{sid}/masters')
async def add_master(sid:str,file:UploadFile=File(...),vin_column:str=Form(...),sales_date:str=Form(''),sales_year:str=Form(''),sales_outlet:str=Form(...),outlet_code:str=Form(''),sales_dealer:str=Form(''),sales_area:str=Form('')):
 s=session(sid);p=await spool(file);rows=added=dup=conflict=unmapped=0
 try:
  h,rs=open_rows(p,file.filename or '')
  try:
   for r in rs:
    rows+=1;v=nvin(r.get(vin_column));y=year(r.get(sales_year) if sales_year else r.get(sales_date));o=norm(r.get(sales_outlet))
    if not v or not y or not 1<=s['crt_year']-y<=8:continue
    om=lookup(r.get(outlet_code) if outlet_code else '',o);d=norm(r.get(sales_dealer)) if sales_dealer else '';a=norm(r.get(sales_area)) if sales_area else ''
    if om:o=om['outlet_name'];d=d or om['dealer'];a=a or om['crt_area']
    elif not d or not a:unmapped+=1
    old=s['vins'].get(v)
    if old:
     dup+=1;conflict+=int(old['sales_outlet']!=o or old['sales_year']!=y);continue
    s['vins'][v]={'sales_year':y,'age':s['crt_year']-y,'sales_outlet':o,'sales_dealer':d,'sales_area':a,'same_outlet':0,'same_dealer':0,'same_area':0,'total':0};added+=1
  finally:
   if hasattr(h,'close'):h.close()
  audit={'file':file.filename,'rows':rows,'added':added,'duplicates':dup,'conflicts':conflict,'unmapped':unmapped};s['master_audit'].append(audit);return audit
 finally:Path(p).unlink(missing_ok=True)
@app.get('/api/sessions/{sid}/population')
def population(sid:str):
 s=session(sid);vs=s['vins'].values()
 return {'total':len(s['vins']),'outlets':sorted({x['sales_outlet'] for x in vs if x['sales_outlet']}),'dealers':sorted({x['sales_dealer'] for x in vs if x['sales_dealer']}),'areas':sorted({x['sales_area'] for x in vs if x['sales_area']}),'level':s['scope_level'],'value':s['scope_value']}
@app.post('/api/sessions/{sid}/population')
def apply_population(sid:str,level:str=Form('ALL'),value:str=Form('')):
 s=session(sid);level=norm(level);value=norm(value)
 if level not in {'ALL','OUTLET','DEALER','AREA'}:raise HTTPException(400,'Invalid scope')
 if s['achievement_audit']:raise HTTPException(400,'Scope is locked after Achievement processing starts')
 s['scope_level']=level;s['scope_value']='' if level=='ALL' else value
 return {'eligible':len(eligible(s)),'level':level,'value':s['scope_value']}
@app.post('/api/sessions/{sid}/achievements')
async def add_achievement(sid:str,file:UploadFile=File(...),vin_column:str=Form(...),service_outlet:str=Form(...),outlet_code:str=Form(''),service_dealer:str=Form('')):
 s=session(sid);target=eligible(s);p=await spool(file);rows=matched=mapped=unmapped=0;seen=set()
 try:
  h,rs=open_rows(p,file.filename or '')
  try:
   for r in rs:
    rows+=1;v=nvin(r.get(vin_column));rec=target.get(v)
    if not rec:continue
    matched+=1;seen.add(v);name=norm(r.get(service_outlet));om=lookup(r.get(outlet_code) if outlet_code else '',name);d=norm(r.get(service_dealer)) if service_dealer else '';a=''
    if om:mapped+=1;name=om['outlet_name'];d=d or om['dealer'];a=om['crt_area']
    else:unmapped+=1
    rec['total']=1;rec['same_outlet']=max(rec['same_outlet'],int(name==rec['sales_outlet']));rec['same_dealer']=max(rec['same_dealer'],int(bool(d) and d==rec['sales_dealer']));rec['same_area']=max(rec['same_area'],int(bool(a) and a==rec['sales_area']))
  finally:
   if hasattr(h,'close'):h.close()
  audit={'file':file.filename,'rows':rows,'matched_rows':matched,'matched_unique_vin':len(seen),'mapped_rows':mapped,'unmapped_rows':unmapped};s['achievement_audit'].append(audit);return audit
 finally:Path(p).unlink(missing_ok=True)
def summarize(s):
 target=eligible(s)
 def agg(records):
  arr=list(records);u=len(arr);d={'uio':u}
  for k in ['same_outlet','same_dealer','same_area','total']:d[k]=sum(x[k] for x in arr);d[k+'_rate']=d[k]/u if u else 0
  return d
 overall=agg(target.values());cohorts=[]
 for age in range(1,9):d=agg(x for x in target.values() if x['age']==age);d.update(age=age,sales_year=s['crt_year']-age);cohorts.append(d)
 return {'session':{k:s[k] for k in ['name','crt_year','period','notes','scope_level','scope_value','created_at']},'overall':overall,'cohorts':cohorts,'audit':{'masters':s['master_audit'],'achievements':s['achievement_audit'],'uploaded_unique_vin':len(s['vins']),'eligible_unique_vin':len(target)}}
@app.post('/api/sessions/{sid}/finalize')
def finalize(sid:str):return summarize(session(sid))
@app.post('/api/sessions/{sid}/save')
def save_result(sid:str,name:str=Form(...),clear_working:bool=Form(True)):
 s=session(sid);p=summarize(s);rid=uuid.uuid4().hex
 with conn() as c:c.execute('INSERT INTO results VALUES(?,?,?,?,?,?,?,?)',(rid,name,s['crt_year'],s['period'],s['scope_level'],s['scope_value'],now(),json.dumps(p)))
 if clear_working:
  with LOCK:SESSIONS.pop(sid,None)
 return {'id':rid,'working_cleared':clear_working}
@app.delete('/api/sessions/{sid}')
def discard(sid:str):
 with LOCK:SESSIONS.pop(sid,None)
 return {'ok':1}
@app.get('/api/results')
def results():
 with conn() as c:return [dict(x) for x in c.execute('SELECT id,name,crt_year,period,scope_level,scope_value,created_at FROM results ORDER BY created_at DESC')]
@app.get('/api/results/{rid}')
def result(rid:str):
 with conn() as c:
  x=c.execute('SELECT * FROM results WHERE id=?',(rid,)).fetchone()
  if not x:raise HTTPException(404,'Saved result not found')
  return {'meta':{k:x[k] for k in x.keys() if k!='payload'},'payload':json.loads(x['payload'])}
@app.delete('/api/results/{rid}')
def result_delete(rid:str):
 with conn() as c:c.execute('DELETE FROM results WHERE id=?',(rid,))
 return {'ok':1}
@app.delete('/api/results')
def results_delete():
 with conn() as c:c.execute('DELETE FROM results')
 return {'ok':1}
