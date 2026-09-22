from __future__ import annotations
import csv, json, os, sqlite3, tempfile, uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List
import polars as pl
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

BASE=Path(__file__).resolve().parent
DB_PATH=Path(os.getenv('DB_PATH', str(BASE/'crt_v3.db')))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
app=FastAPI(title='TAM CRt Analytics Platform',version='3.0.0')
app.mount('/static',StaticFiles(directory=BASE/'static'),name='static')

def db():
 c=sqlite3.connect(DB_PATH,timeout=60); c.row_factory=sqlite3.Row; c.execute('PRAGMA journal_mode=WAL'); return c

def init_db():
 with db() as c:
  c.executescript("""
  CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,name TEXT,crt_year INTEGER,period TEXT,notes TEXT,status TEXT,created_at TEXT,master_files INTEGER DEFAULT 0,master_rows INTEGER DEFAULT 0,achievement_files INTEGER DEFAULT 0,achievement_rows INTEGER DEFAULT 0,outlet_version TEXT);
  CREATE TABLE IF NOT EXISTS session_vins(session_id TEXT,vin TEXT,sales_year INTEGER,age INTEGER,sales_outlet TEXT,sales_dealer TEXT,sales_area TEXT,same_outlet INTEGER DEFAULT 0,same_dealer INTEGER DEFAULT 0,same_area INTEGER DEFAULT 0,total INTEGER DEFAULT 0,PRIMARY KEY(session_id,vin));
  CREATE TABLE IF NOT EXISTS outlet_master(outlet_key TEXT PRIMARY KEY,outlet_name TEXT,dealer TEXT,crt_area TEXT,region TEXT,province TEXT,version TEXT,updated_at TEXT);
  CREATE TABLE IF NOT EXISTS processed_files(session_id TEXT,file_type TEXT,file_name TEXT,rows INTEGER,mapped INTEGER,processed_at TEXT,UNIQUE(session_id,file_type,file_name));
  CREATE TABLE IF NOT EXISTS saved_results(id TEXT PRIMARY KEY,analysis_name TEXT,crt_year INTEGER,period TEXT,notes TEXT,created_at TEXT,payload TEXT);
  """)
init_db()

def norm(v): return ' '.join(str(v or '').strip().upper().split())
def vin(v): return ''.join(str(v or '').strip().upper().split())
def now(): return datetime.now(timezone.utc).isoformat()
async def save_upload(f):
 suffix=Path(f.filename or '').suffix.lower(); h=tempfile.NamedTemporaryFile(delete=False,suffix=suffix)
 try:
  while chunk:=await f.read(1024*1024): h.write(chunk)
  return h.name
 finally: h.close()
def year(v):
 if v is None:return None
 if hasattr(v,'year'):
  try:return int(v.year)
  except:pass
 s=str(v).strip()
 for i in range(max(0,len(s)-3)):
  t=s[i:i+4]
  if t.isdigit() and 1900<=int(t)<=2100:return int(t)
 try:
  n=float(v)
  if 1900<=n<=2100:return int(n)
  if 20000<=n<=80000:
   import datetime as dt
   return (dt.datetime(1899,12,30)+dt.timedelta(days=n)).year
 except:pass
 return None
def records(path,filename,cols=None):
 ext=Path(filename).suffix.lower()
 if ext=='.csv':
  f=open(path,'r',encoding='utf-8-sig',errors='replace',newline=''); return f,csv.DictReader(f)
 if ext in {'.xlsx','.xls','.xlsb'}:
  frame=pl.read_excel(path,engine='calamine')
  if cols: frame=frame.select([x for x in cols if x in frame.columns])
  return frame,frame.iter_rows(named=True)
 raise ValueError('Supported formats: CSV, XLSX, XLS, XLSB')
def headers(path,filename):
 h,it=records(path,filename)
 try:
  if isinstance(it,csv.DictReader): cols=it.fieldnames or []
  else: cols=h.columns
  return cols
 finally:
  if hasattr(h,'close'):h.close()

def need(row,*cols):
 return all(c and c in row for c in cols)

@app.get('/',response_class=HTMLResponse)
def home(): return FileResponse(BASE/'static'/'index.html')
@app.get('/health')
def health(): return {'status':'ok','version':'3.0.0','database':str(DB_PATH)}
@app.post('/api/inspect')
async def inspect(file:UploadFile=File(...)):
 p=await save_upload(file)
 try:return {'filename':file.filename,'columns':headers(p,file.filename or '')}
 except Exception as e:raise HTTPException(400,str(e))
 finally:Path(p).unlink(missing_ok=True)

@app.post('/api/sessions')
def create_session(name:str=Form(...),crt_year:int=Form(...),period:str=Form(''),notes:str=Form('')):
 sid=uuid.uuid4().hex
 with db() as c:c.execute('INSERT INTO sessions(id,name,crt_year,period,notes,status,created_at) VALUES(?,?,?,?,?,?,?)',(sid,name,crt_year,period,notes,'DRAFT',now()))
 return {'session_id':sid}
@app.get('/api/sessions/{sid}')
def session_info(sid:str):
 with db() as c:
  s=c.execute('SELECT * FROM sessions WHERE id=?',(sid,)).fetchone()
  if not s:raise HTTPException(404,'Session not found')
  files=[dict(x) for x in c.execute('SELECT file_type,file_name,rows,mapped,processed_at FROM processed_files WHERE session_id=? ORDER BY processed_at',(sid,))]
  vins=c.execute('SELECT COUNT(*) n FROM session_vins WHERE session_id=?',(sid,)).fetchone()['n']
 return {'session':dict(s),'files':files,'eligible_vin':vins}

@app.post('/api/outlets/upload')
async def upload_outlets(file:UploadFile=File(...),outlet_name:str=Form(...),dealer:str=Form(...),crt_area:str=Form(...),outlet_code:str=Form(''),region:str=Form(''),province:str=Form(''),version:str=Form('Current')):
 p=await save_upload(file); total=valid=conflicts=0
 try:
  h,it=records(p,file.filename or '')
  try:
   with db() as c:
    for r in it:
     total+=1; name=norm(r.get(outlet_name)); key=norm(r.get(outlet_code)) if outlet_code else name; d=norm(r.get(dealer)); a=norm(r.get(crt_area))
     if not key or not name or not d or not a:continue
     old=c.execute('SELECT outlet_name,dealer,crt_area FROM outlet_master WHERE outlet_key=?',(key,)).fetchone()
     if old and (old['dealer']!=d or old['crt_area']!=a):conflicts+=1
     c.execute('INSERT OR REPLACE INTO outlet_master VALUES(?,?,?,?,?,?,?,?)',(key,name,d,a,norm(r.get(region)) if region else '',norm(r.get(province)) if province else '',version,now())); valid+=1
  finally:
   if hasattr(h,'close'):h.close()
  return {'rows':total,'valid':valid,'conflicts':conflicts,'version':version}
 finally:Path(p).unlink(missing_ok=True)
@app.get('/api/outlets/status')
def outlet_status():
 with db() as c:
  x=c.execute('SELECT COUNT(*) n,MAX(version) version,MAX(updated_at) updated_at FROM outlet_master').fetchone()
 return dict(x)
@app.delete('/api/outlets')
def clear_outlets():
 with db() as c:c.execute('DELETE FROM outlet_master')
 return {'deleted':True}

def lookup_outlet(c,key,name):
 k=norm(key) if key else ''
 if k:
  x=c.execute('SELECT * FROM outlet_master WHERE outlet_key=?',(k,)).fetchone()
  if x:return x
 n=norm(name)
 if n:return c.execute('SELECT * FROM outlet_master WHERE outlet_name=?',(n,)).fetchone()
 return None

@app.post('/api/sessions/{sid}/masters')
async def add_master(sid:str,file:UploadFile=File(...),vin_column:str=Form(...),sales_date:str=Form(''),sales_year:str=Form(''),sales_outlet_name:str=Form(...),sales_outlet_code:str=Form(''),sales_dealer:str=Form(''),sales_area:str=Form('')):
 p=await save_upload(file); rows=inserted=dupes=conflicts=unmapped=0
 try:
  with db() as c:
   s=c.execute('SELECT crt_year FROM sessions WHERE id=?',(sid,)).fetchone()
   if not s:raise HTTPException(404,'Session not found')
   crt=s['crt_year']; h,it=records(p,file.filename or '')
   try:
    for r in it:
     rows+=1; v=vin(r.get(vin_column)); y=year(r.get(sales_year) if sales_year else r.get(sales_date))
     if not v or not y or not 1<=crt-y<=8:continue
     outlet=norm(r.get(sales_outlet_name)); d=norm(r.get(sales_dealer)) if sales_dealer else ''; a=norm(r.get(sales_area)) if sales_area else ''
     om=lookup_outlet(c,r.get(sales_outlet_code) if sales_outlet_code else '',outlet)
     if om: outlet=om['outlet_name']; d=d or om['dealer']; a=a or om['crt_area']
     elif not d or not a:unmapped+=1
     old=c.execute('SELECT sales_year,sales_outlet,sales_dealer,sales_area FROM session_vins WHERE session_id=? AND vin=?',(sid,v)).fetchone()
     if old:
      dupes+=1
      if old['sales_year']!=y or (outlet and old['sales_outlet']!=outlet):conflicts+=1
      continue
     c.execute('INSERT INTO session_vins(session_id,vin,sales_year,age,sales_outlet,sales_dealer,sales_area) VALUES(?,?,?,?,?,?,?)',(sid,v,y,crt-y,outlet,d,a));inserted+=1
    c.execute('INSERT OR IGNORE INTO processed_files VALUES(?,?,?,?,?,?)',(sid,'MASTER',file.filename,rows,inserted,now()))
    c.execute('UPDATE sessions SET master_files=master_files+1,master_rows=master_rows+? WHERE id=?',(rows,sid))
   finally:
    if hasattr(h,'close'):h.close()
  return {'rows':rows,'inserted':inserted,'duplicates':dupes,'conflicts':conflicts,'unmapped_origin':unmapped}
 finally:Path(p).unlink(missing_ok=True)

@app.post('/api/sessions/{sid}/achievements')
async def add_achievement(sid:str,file:UploadFile=File(...),vin_column:str=Form(...),service_outlet_name:str=Form(...),service_outlet_code:str=Form(''),service_dealer:str=Form('')):
 p=await save_upload(file); rows=matched=mapped=unmapped=0; seen=set()
 try:
  with db() as c:
   if not c.execute('SELECT 1 FROM sessions WHERE id=?',(sid,)).fetchone():raise HTTPException(404,'Session not found')
   h,it=records(p,file.filename or '')
   try:
    for r in it:
     rows+=1; v=vin(r.get(vin_column))
     if not v:continue
     sv=c.execute('SELECT sales_outlet,sales_dealer,sales_area FROM session_vins WHERE session_id=? AND vin=?',(sid,v)).fetchone()
     if not sv:continue
     matched+=1; seen.add(v); service_name=norm(r.get(service_outlet_name)); om=lookup_outlet(c,r.get(service_outlet_code) if service_outlet_code else '',service_name)
     sd=norm(r.get(service_dealer)) if service_dealer else ''; sa=''
     if om: mapped+=1; service_name=om['outlet_name'];sd=sd or om['dealer'];sa=om['crt_area']
     else:unmapped+=1
     so=int(bool(service_name and service_name==sv['sales_outlet'])); sde=int(bool(sd and sd==sv['sales_dealer'])); sar=int(bool(sa and sa==sv['sales_area']))
     c.execute('UPDATE session_vins SET total=1,same_outlet=MAX(same_outlet,?),same_dealer=MAX(same_dealer,?),same_area=MAX(same_area,?) WHERE session_id=? AND vin=?',(so,sde,sar,sid,v))
    c.execute('INSERT OR IGNORE INTO processed_files VALUES(?,?,?,?,?,?)',(sid,'ACHIEVEMENT',file.filename,rows,len(seen),now()))
    c.execute('UPDATE sessions SET achievement_files=achievement_files+1,achievement_rows=achievement_rows+? WHERE id=?',(rows,sid))
   finally:
    if hasattr(h,'close'):h.close()
  return {'rows':rows,'matched_rows':matched,'matched_unique_vin':len(seen),'mapped_rows':mapped,'unmapped_rows':unmapped}
 finally:Path(p).unlink(missing_ok=True)

def result_payload(c,sid):
 s=c.execute('SELECT * FROM sessions WHERE id=?',(sid,)).fetchone()
 if not s:raise HTTPException(404,'Session not found')
 overall=c.execute('SELECT COUNT(*) uio,SUM(same_outlet) same_outlet,SUM(same_dealer) same_dealer,SUM(same_area) same_area,SUM(total) total FROM session_vins WHERE session_id=?',(sid,)).fetchone()
 cohorts=[]
 for age in range(1,9):
  x=c.execute('SELECT COUNT(*) uio,SUM(same_outlet) same_outlet,SUM(same_dealer) same_dealer,SUM(same_area) same_area,SUM(total) total FROM session_vins WHERE session_id=? AND age=?',(sid,age)).fetchone(); d=dict(x);d.update(age=age,sales_year=s['crt_year']-age);cohorts.append(d)
 files=[dict(x) for x in c.execute('SELECT file_type,file_name,rows,mapped,processed_at FROM processed_files WHERE session_id=?',(sid,))]
 out=dict(overall);u=out['uio'] or 0
 for k in ['same_outlet','same_dealer','same_area','total']:out[k]=out[k] or 0;out[k+'_rate']=out[k]/u if u else 0
 return {'session':dict(s),'overall':out,'cohorts':cohorts,'files':files}
@app.post('/api/sessions/{sid}/finalize')
def finalize(sid:str):
 with db() as c:
  payload=result_payload(c,sid);c.execute("UPDATE sessions SET status='FINALIZED' WHERE id=?",(sid,))
 return payload
@app.post('/api/sessions/{sid}/save')
def save_result(sid:str,analysis_name:str=Form(...),replace_existing:bool=Form(False)):
 with db() as c:
  payload=result_payload(c,sid); rid=uuid.uuid4().hex
  if replace_existing:c.execute('DELETE FROM saved_results WHERE analysis_name=?',(analysis_name,))
  c.execute('INSERT INTO saved_results VALUES(?,?,?,?,?,?,?)',(rid,analysis_name,payload['session']['crt_year'],payload['session']['period'],payload['session']['notes'],now(),json.dumps(payload)))
 return {'id':rid}
@app.get('/api/results')
def results():
 with db() as c:return [dict(x) for x in c.execute('SELECT id,analysis_name,crt_year,period,notes,created_at FROM saved_results ORDER BY created_at DESC')]
@app.get('/api/results/{rid}')
def get_result(rid:str):
 with db() as c:
  x=c.execute('SELECT * FROM saved_results WHERE id=?',(rid,)).fetchone()
  if not x:raise HTTPException(404,'Saved result not found')
 return {'meta':{k:x[k] for k in x.keys() if k!='payload'},'payload':json.loads(x['payload'])}
@app.delete('/api/results/{rid}')
def delete_result(rid:str):
 with db() as c:c.execute('DELETE FROM saved_results WHERE id=?',(rid,))
 return {'deleted':True}
@app.delete('/api/results')
def delete_all():
 with db() as c:c.execute('DELETE FROM saved_results')
 return {'deleted':True}
@app.delete('/api/sessions/{sid}')
def delete_session(sid:str):
 with db() as c:c.execute('DELETE FROM session_vins WHERE session_id=?',(sid,));c.execute('DELETE FROM processed_files WHERE session_id=?',(sid,));c.execute('DELETE FROM sessions WHERE id=?',(sid,))
 return {'deleted':True}
