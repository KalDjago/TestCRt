import csv,json,os,sqlite3,tempfile,uuid
from datetime import datetime,timezone
from pathlib import Path
from fastapi import FastAPI,File,Form,HTTPException,UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import polars as pl

BASE=Path(__file__).parent; DB=Path(os.getenv('DB_PATH',str(BASE/'crt_v31.db'))); DB.parent.mkdir(parents=True,exist_ok=True)
app=FastAPI(title='TAM CRt Analytics V3.1'); app.mount('/static',StaticFiles(directory=BASE/'static'),name='static')
def con(): c=sqlite3.connect(DB,timeout=120);c.row_factory=sqlite3.Row;c.execute('PRAGMA journal_mode=WAL');return c
def now():return datetime.now(timezone.utc).isoformat()
def norm(x):return ' '.join(str(x or '').strip().upper().split())
def nvin(x):return ''.join(str(x or '').strip().upper().split())
with con() as c:c.executescript('''
CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,name TEXT,crt_year INT,period TEXT,notes TEXT,status TEXT,filter_level TEXT DEFAULT 'ALL',filter_value TEXT DEFAULT '',created_at TEXT,master_files INT DEFAULT 0,achievement_files INT DEFAULT 0);
CREATE TABLE IF NOT EXISTS vins(session_id TEXT,vin TEXT,sales_year INT,age INT,sales_outlet TEXT,sales_dealer TEXT,sales_area TEXT,same_outlet INT DEFAULT 0,same_dealer INT DEFAULT 0,same_area INT DEFAULT 0,total INT DEFAULT 0,PRIMARY KEY(session_id,vin));
CREATE TABLE IF NOT EXISTS outlets(outlet_key TEXT PRIMARY KEY,outlet_name TEXT,dealer TEXT,crt_area TEXT,region TEXT,province TEXT,version TEXT,updated_at TEXT);
CREATE TABLE IF NOT EXISTS files(session_id TEXT,file_type TEXT,file_name TEXT,rows INT,matched INT,processed_at TEXT,UNIQUE(session_id,file_type,file_name));
CREATE TABLE IF NOT EXISTS results(id TEXT PRIMARY KEY,name TEXT,crt_year INT,period TEXT,created_at TEXT,payload TEXT);
''')
async def spool(f):
 h=tempfile.NamedTemporaryFile(delete=False,suffix=Path(f.filename or '').suffix)
 try:
  while x:=await f.read(1024*1024):h.write(x)
  return h.name
 finally:h.close()
def rows(path,name):
 ext=Path(name).suffix.lower()
 if ext=='.csv':
  h=open(path,encoding='utf-8-sig',errors='replace',newline='');return h,csv.DictReader(h)
 if ext in {'.xlsx','.xls','.xlsb'}:
  d=pl.read_excel(path,engine='calamine');return d,d.iter_rows(named=True)
 raise ValueError('Supported formats: CSV, XLSX, XLS, XLSB')
def cols(path,name):
 h,r=rows(path,name)
 try:return r.fieldnames or [] if isinstance(r,csv.DictReader) else h.columns
 finally:
  if hasattr(h,'close'):h.close()
def yr(x):
 if hasattr(x,'year'):return int(x.year)
 s=str(x or '')
 for i in range(max(0,len(s)-3)):
  z=s[i:i+4]
  if z.isdigit() and 1900<=int(z)<=2100:return int(z)
 try:
  n=float(x)
  if 20000<=n<=80000:
   import datetime as dt
   return (dt.datetime(1899,12,30)+dt.timedelta(days=n)).year
 except:pass
 return None
def outlet(c,key,name):
 if norm(key):
  x=c.execute('SELECT * FROM outlets WHERE outlet_key=?',(norm(key),)).fetchone()
  if x:return x
 return c.execute('SELECT * FROM outlets WHERE outlet_name=?',(norm(name),)).fetchone() if norm(name) else None
def scope(s):
 level=s['filter_level'];value=s['filter_value']
 if level=='OUTLET':return ' AND sales_outlet=?',[value]
 if level=='DEALER':return ' AND sales_dealer=?',[value]
 if level=='AREA':return ' AND sales_area=?',[value]
 return '',[]
@app.get('/')
def home():return FileResponse(BASE/'static'/'index.html')
@app.get('/health')
def health():return {'status':'ok','version':'3.1.0','database':str(DB)}
@app.post('/api/inspect')
async def inspect(file:UploadFile=File(...)):
 p=await spool(file)
 try:return {'columns':cols(p,file.filename or '')}
 finally:Path(p).unlink(missing_ok=True)
@app.post('/api/outlets')
async def put_outlets(file:UploadFile=File(...),outlet_name:str=Form(...),dealer:str=Form(...),crt_area:str=Form(...),outlet_code:str=Form(''),region:str=Form(''),province:str=Form(''),version:str=Form('Current')):
 p=await spool(file);n=bad=0
 try:
  h,rs=rows(p,file.filename or '')
  try:
   with con() as c:
    for r in rs:
     name=norm(r.get(outlet_name));key=norm(r.get(outlet_code)) if outlet_code else name;d=norm(r.get(dealer));a=norm(r.get(crt_area))
     if not all([key,name,d,a]):bad+=1;continue
     c.execute('INSERT OR REPLACE INTO outlets VALUES(?,?,?,?,?,?,?,?)',(key,name,d,a,norm(r.get(region)) if region else '',norm(r.get(province)) if province else '',version,now()));n+=1
  finally:
   if hasattr(h,'close'):h.close()
  return {'saved':n,'invalid':bad}
 finally:Path(p).unlink(missing_ok=True)
@app.get('/api/outlets')
def outlet_status():
 with con() as c:return dict(c.execute('SELECT COUNT(*) count,MAX(version) version,MAX(updated_at) updated_at FROM outlets').fetchone())
@app.delete('/api/outlets')
def outlet_delete():
 with con() as c:c.execute('DELETE FROM outlets')
 return {'ok':1}
@app.post('/api/sessions')
def create(name:str=Form(...),crt_year:int=Form(...),period:str=Form(''),notes:str=Form('')):
 sid=uuid.uuid4().hex
 with con() as c:c.execute('INSERT INTO sessions(id,name,crt_year,period,notes,status,created_at) VALUES(?,?,?,?,?,?,?)',(sid,name,crt_year,period,notes,'DRAFT',now()))
 return {'id':sid}
@app.post('/api/sessions/{sid}/masters')
async def masters(sid:str,file:UploadFile=File(...),vin_column:str=Form(...),sales_date:str=Form(''),sales_year:str=Form(''),sales_outlet:str=Form(...),outlet_code:str=Form(''),sales_dealer:str=Form(''),sales_area:str=Form('')):
 p=await spool(file);total=added=dup=conflict=unmapped=0
 try:
  with con() as c:
   s=c.execute('SELECT * FROM sessions WHERE id=?',(sid,)).fetchone()
   if not s:raise HTTPException(404,'Session not found')
   h,rs=rows(p,file.filename or '')
   try:
    for r in rs:
     total+=1;v=nvin(r.get(vin_column));y=yr(r.get(sales_year) if sales_year else r.get(sales_date));o=norm(r.get(sales_outlet))
     if not v or not y or not 1<=s['crt_year']-y<=8:continue
     om=outlet(c,r.get(outlet_code) if outlet_code else '',o);d=norm(r.get(sales_dealer)) if sales_dealer else '';a=norm(r.get(sales_area)) if sales_area else ''
     if om:o=om['outlet_name'];d=d or om['dealer'];a=a or om['crt_area']
     elif not d or not a:unmapped+=1
     old=c.execute('SELECT * FROM vins WHERE session_id=? AND vin=?',(sid,v)).fetchone()
     if old:
      dup+=1;conflict+=int(old['sales_outlet']!=o or old['sales_year']!=y);continue
     c.execute('INSERT INTO vins(session_id,vin,sales_year,age,sales_outlet,sales_dealer,sales_area) VALUES(?,?,?,?,?,?,?)',(sid,v,y,s['crt_year']-y,o,d,a));added+=1
    c.execute('INSERT OR IGNORE INTO files VALUES(?,?,?,?,?,?)',(sid,'MASTER',file.filename,total,added,now()));c.execute('UPDATE sessions SET master_files=master_files+1 WHERE id=?',(sid,))
   finally:
    if hasattr(h,'close'):h.close()
  return {'rows':total,'added':added,'duplicates':dup,'conflicts':conflict,'unmapped':unmapped}
 finally:Path(p).unlink(missing_ok=True)
@app.get('/api/sessions/{sid}/population')
def pop(sid:str):
 with con() as c:
  s=c.execute('SELECT * FROM sessions WHERE id=?',(sid,)).fetchone()
  if not s:raise HTTPException(404,'Session not found')
  def vals(col):return [x[0] for x in c.execute(f"SELECT DISTINCT {col} FROM vins WHERE session_id=? AND {col}<>'' ORDER BY {col}",(sid,))]
  return {'total':c.execute('SELECT COUNT(*) FROM vins WHERE session_id=?',(sid,)).fetchone()[0],'outlets':vals('sales_outlet'),'dealers':vals('sales_dealer'),'areas':vals('sales_area'),'filter_level':s['filter_level'],'filter_value':s['filter_value']}
@app.post('/api/sessions/{sid}/population')
def set_pop(sid:str,level:str=Form('ALL'),value:str=Form('')):
 level=norm(level);value=norm(value)
 if level not in {'ALL','OUTLET','DEALER','AREA'}:raise HTTPException(400,'Invalid filter level')
 with con() as c:
  s=c.execute('SELECT * FROM sessions WHERE id=?',(sid,)).fetchone()
  if not s:raise HTTPException(404,'Session not found')
  if s['achievement_files']>0:raise HTTPException(400,'Population filter is locked after Achievement processing starts')
  c.execute('UPDATE sessions SET filter_level=?,filter_value=? WHERE id=?',(level,'' if level=='ALL' else value,sid));where,args=scope({'filter_level':level,'filter_value':value});n=c.execute('SELECT COUNT(*) FROM vins WHERE session_id=?'+where,[sid]+args).fetchone()[0]
 return {'eligible':n,'level':level,'value':value}
@app.post('/api/sessions/{sid}/achievements')
async def achievements(sid:str,file:UploadFile=File(...),vin_column:str=Form(...),service_outlet:str=Form(...),outlet_code:str=Form(''),service_dealer:str=Form('')):
 p=await spool(file);total=match=mapped=unmapped=0;seen=set()
 try:
  with con() as c:
   s=c.execute('SELECT * FROM sessions WHERE id=?',(sid,)).fetchone();where,args=scope(s)
   if not s:raise HTTPException(404,'Session not found')
   h,rs=rows(p,file.filename or '')
   try:
    for r in rs:
     total+=1;v=nvin(r.get(vin_column));sv=c.execute('SELECT * FROM vins WHERE session_id=? AND vin=?'+where,[sid,v]+args).fetchone()
     if not sv:continue
     match+=1;seen.add(v);name=norm(r.get(service_outlet));om=outlet(c,r.get(outlet_code) if outlet_code else '',name);d=norm(r.get(service_dealer)) if service_dealer else '';a=''
     if om:mapped+=1;name=om['outlet_name'];d=d or om['dealer'];a=om['crt_area']
     else:unmapped+=1
     c.execute('UPDATE vins SET total=1,same_outlet=MAX(same_outlet,?),same_dealer=MAX(same_dealer,?),same_area=MAX(same_area,?) WHERE session_id=? AND vin=?',(int(name==sv['sales_outlet']),int(bool(d) and d==sv['sales_dealer']),int(bool(a) and a==sv['sales_area']),sid,v))
    c.execute('INSERT OR IGNORE INTO files VALUES(?,?,?,?,?,?)',(sid,'ACHIEVEMENT',file.filename,total,len(seen),now()));c.execute('UPDATE sessions SET achievement_files=achievement_files+1 WHERE id=?',(sid,))
   finally:
    if hasattr(h,'close'):h.close()
  return {'rows':total,'matched_unique_vin':len(seen),'mapped_rows':mapped,'unmapped_rows':unmapped}
 finally:Path(p).unlink(missing_ok=True)
def payload(c,sid):
 s=c.execute('SELECT * FROM sessions WHERE id=?',(sid,)).fetchone();where,args=scope(s)
 def agg(extra='',xargs=[]):return dict(c.execute('SELECT COUNT(*) uio,COALESCE(SUM(same_outlet),0) same_outlet,COALESCE(SUM(same_dealer),0) same_dealer,COALESCE(SUM(same_area),0) same_area,COALESCE(SUM(total),0) total FROM vins WHERE session_id=?'+where+extra,[sid]+args+xargs).fetchone())
 o=agg();u=o['uio'];o.update({k+'_rate':o[k]/u if u else 0 for k in ['same_outlet','same_dealer','same_area','total']})
 cs=[]
 for age in range(1,9):d=agg(' AND age=?',[age]);d.update(age=age,sales_year=s['crt_year']-age);cs.append(d)
 return {'session':dict(s),'overall':o,'cohorts':cs,'files':[dict(x) for x in c.execute('SELECT * FROM files WHERE session_id=?',(sid,))]}
@app.post('/api/sessions/{sid}/finalize')
def finalize(sid:str):
 with con() as c:return payload(c,sid)
@app.post('/api/sessions/{sid}/save')
def save_result(sid:str,name:str=Form(...)):
 with con() as c:
  p=payload(c,sid);rid=uuid.uuid4().hex;c.execute('INSERT INTO results VALUES(?,?,?,?,?,?)',(rid,name,p['session']['crt_year'],p['session']['period'],now(),json.dumps(p)))
 return {'id':rid}
@app.get('/api/results')
def list_results():
 with con() as c:return [dict(x) for x in c.execute('SELECT id,name,crt_year,period,created_at FROM results ORDER BY created_at DESC')]
@app.get('/api/results/{rid}')
def get_result(rid:str):
 with con() as c:
  x=c.execute('SELECT * FROM results WHERE id=?',(rid,)).fetchone()
  if not x:raise HTTPException(404,'Not found')
  return {'meta':dict(x),'payload':json.loads(x['payload'])}
@app.delete('/api/results/{rid}')
def del_result(rid:str):
 with con() as c:c.execute('DELETE FROM results WHERE id=?',(rid,))
 return {'ok':1}
@app.delete('/api/results')
def del_all():
 with con() as c:c.execute('DELETE FROM results')
 return {'ok':1}
