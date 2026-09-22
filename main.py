import os, tempfile, uuid
from pathlib import Path
from threading import RLock
import polars as pl
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

BASE=Path(__file__).parent
app=FastAPI(title='TAM CRt Analytics V2 Reborn',version='2.5.0')
app.mount('/static',StaticFiles(directory=BASE/'static'),name='static')
SESSIONS={}; LOCK=RLock()

def norm_expr(c):return pl.col(c).cast(pl.String,strict=False).fill_null('').str.strip_chars().str.to_uppercase().str.replace_all(r'\s+',' ')
def vin_expr(c):return pl.col(c).cast(pl.String,strict=False).fill_null('').str.strip_chars().str.to_uppercase().str.replace_all(r'\s+','')
def norm(x):return ' '.join(str(x or '').strip().upper().split())
async def spool(f):
 h=tempfile.NamedTemporaryFile(delete=False,suffix=Path(f.filename or '').suffix)
 try:
  while b:=await f.read(1024*1024):h.write(b)
  return h.name
 finally:h.close()
def frame(path,name,columns=None):
 ext=Path(name).suffix.lower()
 if ext=='.csv':
  return pl.read_csv(path,columns=columns,infer_schema_length=10000,ignore_errors=True,encoding='utf8-lossy')
 if ext in {'.xlsx','.xls','.xlsb'}:
  d=pl.read_excel(path,engine='calamine')
  return d.select([c for c in columns if c in d.columns]) if columns else d
 raise ValueError('Supported formats: CSV, XLSX, XLS, XLSB')
def year_expr(c):
 raw=pl.col(c)
 return pl.when(raw.cast(pl.Date,strict=False).is_not_null()).then(raw.cast(pl.Date,strict=False).dt.year()).otherwise(raw.cast(pl.String,strict=False).str.extract(r'((?:19|20)\d{2})',1).cast(pl.Int32,strict=False))
def sess(sid):
 s=SESSIONS.get(sid)
 if not s:raise HTTPException(404,'Working session not found. Start a new analysis.')
 return s
@app.get('/')
def home():return FileResponse(BASE/'static'/'index.html')
@app.get('/health')
def health():return {'status':'ok','version':'2.5.0','engine':'V2 memory-first Polars','working_sessions':len(SESSIONS)}
@app.post('/api/inspect')
async def inspect(file:UploadFile=File(...)):
 p=await spool(file)
 try:return {'columns':frame(p,file.filename or '').columns}
 finally:Path(p).unlink(missing_ok=True)
@app.post('/api/sessions')
def create(name:str=Form(...),crt_year:int=Form(...),period:str=Form(''),level:str=Form('Outlet')):
 sid=uuid.uuid4().hex
 with LOCK:SESSIONS[sid]={'name':name,'crt_year':crt_year,'period':period,'level':level.upper(),'master':None,'scope':'','flags':{},'master_audit':[],'ach_audit':[]}
 return {'id':sid}
@app.post('/api/sessions/{sid}/master')
async def master(sid:str,file:UploadFile=File(...),vin_column:str=Form(...),sales_date:str=Form(''),sales_year:str=Form(''),sales_origin:str=Form(...)):
 s=sess(sid);p=await spool(file)
 try:
  cols=list(dict.fromkeys([vin_column,sales_origin,sales_year or sales_date]));d=frame(p,file.filename or '',cols)
  missing=[c for c in cols if c not in d.columns]
  if missing:raise ValueError('Missing master columns: '+', '.join(missing))
  y=year_expr(sales_year or sales_date)
  cleaned=(d.with_columns([vin_expr(vin_column).alias('VIN'),norm_expr(sales_origin).alias('ORIGIN'),y.alias('SALES_YEAR')])
   .select(['VIN','ORIGIN','SALES_YEAR']).filter((pl.col('VIN')!='')&(pl.col('ORIGIN')!='')&pl.col('SALES_YEAR').is_not_null())
   .with_columns((pl.lit(s['crt_year'])-pl.col('SALES_YEAR')).alias('AGE')).filter(pl.col('AGE').is_between(1,8)))
  before=cleaned.height; conflict=(cleaned.group_by('VIN').agg(pl.col('ORIGIN').n_unique().alias('N')).filter(pl.col('N')>1).height)
  cleaned=cleaned.unique('VIN',keep='first')
  s['master']=cleaned if s['master'] is None else pl.concat([s['master'],cleaned],how='vertical_relaxed').unique('VIN',keep='first')
  a={'file':file.filename,'input_rows':d.height,'eligible_rows':before,'added_unique':cleaned.height,'duplicate_rows':before-cleaned.height,'conflicting_vin':conflict};s['master_audit'].append(a);return a
 except Exception as e:raise HTTPException(400,f'{type(e).__name__}: {e}')
 finally:Path(p).unlink(missing_ok=True)
@app.get('/api/sessions/{sid}/origins')
def origins(sid:str):
 s=sess(sid)
 if s['master'] is None:raise HTTPException(400,'Process Master UIO first')
 return {'total':s['master'].height,'values':s['master']['ORIGIN'].unique().sort().to_list(),'scope':s['scope']}
@app.post('/api/sessions/{sid}/scope')
def scope(sid:str,value:str=Form('')):
 s=sess(sid);s['scope']=norm(value);s['flags']={};s['ach_audit']=[]
 n=s['master'].filter(pl.col('ORIGIN')==s['scope']).height if s['scope'] else s['master'].height
 return {'eligible':n,'scope':s['scope'] or 'ALL'}
@app.post('/api/sessions/{sid}/achievement')
async def achievement(sid:str,file:UploadFile=File(...),vin_column:str=Form(...),service_destination:str=Form(...)):
 s=sess(sid)
 if s['master'] is None:raise HTTPException(400,'Process Master UIO first')
 p=await spool(file)
 try:
  d=frame(p,file.filename or '',[vin_column,service_destination]);missing=[c for c in [vin_column,service_destination] if c not in d.columns]
  if missing:raise ValueError('Missing achievement columns: '+', '.join(missing))
  eligible=s['master'].filter(pl.col('ORIGIN')==s['scope']) if s['scope'] else s['master']
  ach=(d.with_columns([vin_expr(vin_column).alias('VIN'),norm_expr(service_destination).alias('DEST')]).select(['VIN','DEST']).filter(pl.col('VIN')!='').unique(['VIN','DEST']))
  matched=(ach.join(eligible.select(['VIN','ORIGIN']),on='VIN',how='inner').with_columns((pl.col('DEST')==pl.col('ORIGIN')).cast(pl.Int8).alias('SAME'))
   .group_by('VIN').agg([pl.max('SAME').alias('SAME'),pl.lit(1).alias('TOTAL')]))
  for v,sm,tot in matched.iter_rows():
   old=s['flags'].get(v,(0,0));s['flags'][v]=(max(old[0],sm),max(old[1],tot))
  a={'file':file.filename,'input_rows':d.height,'unique_vin_destination':ach.height,'matched_unique_vin':matched.height};s['ach_audit'].append(a);return a
 except Exception as e:raise HTTPException(400,f'{type(e).__name__}: {e}')
 finally:Path(p).unlink(missing_ok=True)
@app.post('/api/sessions/{sid}/finalize')
def finalize(sid:str):
 s=sess(sid);m=s['master'].filter(pl.col('ORIGIN')==s['scope']) if s['scope'] else s['master']
 f=pl.DataFrame({'VIN':list(s['flags']), 'SAME':[x[0] for x in s['flags'].values()], 'TOTAL':[x[1] for x in s['flags'].values()]}) if s['flags'] else pl.DataFrame({'VIN':[], 'SAME':[], 'TOTAL':[]},schema={'VIN':pl.String,'SAME':pl.Int64,'TOTAL':pl.Int64})
 det=m.join(f,on='VIN',how='left').with_columns([pl.col('SAME').fill_null(0),pl.col('TOTAL').fill_null(0)])
 out=[]
 for age in range(1,9):
  c=det.filter(pl.col('AGE')==age);u=c.height;sm=int(c['SAME'].sum());tt=int(c['TOTAL'].sum());out.append({'age':age,'sales_year':s['crt_year']-age,'uio':u,'same_count':sm,'total_count':tt,'same_rate':sm/u if u else 0,'total_rate':tt/u if u else 0,'gap':(tt-sm)/u if u else 0})
 u=det.height;sm=int(det['SAME'].sum());tt=int(det['TOTAL'].sum())
 return {'name':s['name'],'period':s['period'],'level':s['level'],'scope':s['scope'] or 'ALL','overall':{'uio':u,'same_count':sm,'total_count':tt,'same_rate':sm/u if u else 0,'total_rate':tt/u if u else 0},'cohorts':out,'audit':{'masters':s['master_audit'],'achievements':s['ach_audit']}}
@app.delete('/api/sessions/{sid}')
def discard(sid:str):SESSIONS.pop(sid,None);return {'ok':1}
