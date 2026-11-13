"""Version-bound catalog override; no background process or network requests."""
import argparse, copy, hashlib, json, os, queue, subprocess, sys, tempfile, threading, time
from pathlib import Path
try:
    import tomllib
except ImportError:
    import tomli as tomllib

ROOT=Path(os.environ.get('LOCALAPPDATA', Path.home()/'.local'/'share'))/'codex-title-manager'
CONFIG=Path(os.environ.get('CODEX_HOME', Path.home()/'.codex'))/'config.toml'
CATALOG=ROOT/'catalog.json'
MANIFEST=ROOT/'manifest.json'
PRIVATE=ROOT/'private'
STATE=PRIVATE/'state.json'
EXE_OVERRIDE=None
REMOVED_MODEL='gpt-6-luna'
FALLBACK_MODEL='gpt-6.1-sol'
KEY='model_catalog_json'

def configure(exe=None, codex_home=None, data_dir=None, removed_model=None, fallback_model=None):
    global ROOT, CONFIG, CATALOG, MANIFEST, PRIVATE, STATE, EXE_OVERRIDE, REMOVED_MODEL, FALLBACK_MODEL
    if exe: EXE_OVERRIDE=exe
    if codex_home: CONFIG=Path(codex_home).expanduser().resolve()/'config.toml'
    if data_dir: ROOT=Path(data_dir).expanduser().resolve()
    CATALOG=ROOT/'catalog.json'; MANIFEST=ROOT/'manifest.json'
    PRIVATE=ROOT/'private'; STATE=PRIVATE/'state.json'
    if removed_model: REMOVED_MODEL=removed_model
    if fallback_model: FALLBACK_MODEL=fallback_model

def ensure_data_dir():
    ROOT.mkdir(parents=True,exist_ok=True)
    if os.name=='nt':
        sid=subprocess.check_output(['whoami','/user','/fo','csv','/nh'],text=True).strip().split(',')[-1].strip('"')
        subprocess.run(['icacls',str(ROOT),'/inheritance:r','/grant:r','*'+sid+':(OI)(CI)F'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    else: ROOT.chmod(0o700)

def digest(data): return hashlib.sha256(data).hexdigest()
def read_json(path): return json.loads(path.read_text(encoding='utf-8'))
def write_json(path, value): atomic(path,(json.dumps(value,ensure_ascii=False,indent=2)+'\n').encode('utf-8'))
def filter_catalog(value):
    result=copy.deepcopy(value)
    result['models']=[m for m in result['models'] if m['slug']!=REMOVED_MODEL]
    return result

def parsed(raw): return tomllib.loads(raw.decode('utf-8-sig'))
def setting_line(path, newline): return (KEY+' = '+json.dumps(path,ensure_ascii=False)+newline).encode('utf-8')
def add_setting(raw,path):
    current=parsed(raw).get(KEY)
    if current is not None:
        if current==path: return raw
        raise ValueError('Existing user catalog: refusing overwrite')
    bom=b'\xef\xbb\xbf' if raw.startswith(b'\xef\xbb\xbf') else b''
    newline='\r\n' if b'\r\n' in raw else '\n'
    result=bom+setting_line(path,newline)+raw[len(bom):]
    if parsed(result).get(KEY)!=path: raise ValueError('Cannot safely insert setting')
    return result

def remove_setting(raw,path):
    if parsed(raw).get(KEY)!=path: return raw
    bom=b'\xef\xbb\xbf' if raw.startswith(b'\xef\xbb\xbf') else b''
    rest=raw[len(bom):]
    for newline in ('\r\n','\n'):
        prefix=setting_line(path,newline)
        if rest.startswith(prefix): return bom+rest[len(prefix):]
    raise ValueError('Setting formatting changed; refusing to remove')

def atomic(path,data):
    descriptor,name=tempfile.mkstemp(prefix=path.name+'.',suffix='.repair-tmp',dir=path.parent)
    temp=Path(name)
    try:
        with os.fdopen(descriptor,'wb') as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
        os.replace(temp,path)
    finally:
        if temp.exists(): temp.unlink()

def active_cli():
    if EXE_OVERRIDE: return Path(EXE_OVERRIDE).resolve()
    config=parsed(CONFIG.read_bytes())
    path=config.get('mcp_servers',{}).get('node_repl',{}).get('env',{}).get('CODEX_CLI_PATH')
    if not path: raise ValueError('Cannot resolve CLI: pass --exe explicitly')
    return Path(path).resolve()

def desktop_package():
    command="Get-AppxPackage -Name OpenAI.Codex | Select-Object Name,Version,InstallLocation | ConvertTo-Json -Compress"
    value=json.loads(subprocess.check_output(['powershell','-NoProfile','-Command',command],text=True,encoding='utf-8'))
    if not isinstance(value,dict) or not value.get('Version'): raise ValueError('Cannot identify current desktop package')
    return value

def validate_source():
    m=read_json(MANIFEST)
    current=active_cli()
    if str(current)!=m['exe']: raise ValueError('Active CLI path changed: prepare and verify again')
    if desktop_package()!=m.get('desktop_package'): raise ValueError('Desktop package changed: prepare and verify again')
    if digest(current.read_bytes())!=m['exe_sha256']: raise ValueError('CLI version changed: run prepare and verify again')
    if digest(CATALOG.read_bytes())!=m['catalog_sha256']: raise ValueError('Catalog changed: run prepare and verify again')
    return m

def prepare():
    ensure_data_dir()
    if STATE.exists() and (read_json(STATE).get('active') or read_json(STATE).get('pending')): raise ValueError('Restore the active repair before preparing another catalog')
    if REMOVED_MODEL==FALLBACK_MODEL: raise ValueError('Removed and fallback models must differ')
    exe=active_cli()
    if (ROOT/'verification.json').exists(): (ROOT/'verification.json').unlink()
    data=exe.read_bytes(); marker=b'"models": ['
    if data.count(marker)!=1: raise ValueError('Embedded catalog marker is not unique')
    start=data.rfind(b'{',0,data.index(marker))
    original,end=json.JSONDecoder().raw_decode(data[start:].decode('utf-8',errors='replace'))
    candidate=filter_catalog(original)
    if FALLBACK_MODEL not in {x['slug'] for x in candidate['models']}: raise ValueError('Fallback model is absent')
    if len(original['models'])-len(candidate['models'])!=1: raise ValueError('Expected exactly one removed-model entry')
    write_json(ROOT/'original_catalog.json',original); write_json(CATALOG,candidate)
    m={'exe':str(exe),'desktop_package':desktop_package(),'exe_sha256':digest(data),'catalog_sha256':digest(CATALOG.read_bytes()),'original_count':len(original['models']),'candidate_count':len(candidate['models']),'removed':[REMOVED_MODEL],'fallback':FALLBACK_MODEL,'prepared_at':time.strftime('%Y-%m-%dT%H:%M:%S%z')}
    write_json(MANIFEST,m); return m

def verify():
    m=validate_source()
    verify_env=os.environ.copy(); verify_env['CODEX_HOME']=str(CONFIG.parent)
    p=subprocess.Popen([m['exe'],'app-server','--stdio','-c',KEY+'='+json.dumps(str(CATALOG))],env=verify_env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,encoding='utf-8')
    q=queue.Queue()
    def reader():
        for line in p.stdout:
            try: q.put(json.loads(line))
            except ValueError: pass
    threading.Thread(target=reader,daemon=True).start()
    def send(v): p.stdin.write(json.dumps(v)+'\n'); p.stdin.flush()
    def response(i):
        until=time.monotonic()+45
        while time.monotonic()<until:
            v=q.get(timeout=max(.01,until-time.monotonic()))
            if v.get('id')==i:
                if 'error' in v: raise ValueError('app-server rejected request: '+json.dumps(v['error']))
                return v['result']
        raise TimeoutError('app-server response timeout')
    try:
        send({'id':1,'method':'initialize','params':{'clientInfo':{'name':'catalog-repair-check','version':'1.0'},'capabilities':{'experimentalApi':True}}}); init=response(1)
        send({'method':'initialized','params':{}})
        send({'id':2,'method':'model/list','params':{'includeHidden':True}}); listing=response(2)
    finally:
        p.kill(); p.wait(); p.stdin.close(); p.stdout.close()
    write_json(ROOT/'model_list.json',listing)
    models=listing['data']; expected=read_json(CATALOG)['models']
    actual={v['model']:v for v in models}
    expected_slugs={v['slug'] for v in expected}
    if set(actual)!=expected_slugs: raise ValueError('Catalog was merged or omitted models; see model_list.json')
    defaults=[v['model'] for v in models if v.get('isDefault')]
    if defaults!=[m['fallback']]: raise ValueError('Unexpected default model')
    original=read_json(ROOT/'original_catalog.json')
    if dict(original,models=[x for x in original['models'] if x['slug'] not in m['removed']])!=read_json(CATALOG): raise ValueError('Model capability metadata changed')
    evidence={'passed':True,'models':len(models),'removed_model_absent':m['removed'][0] not in actual,'defaults':defaults,'all_remaining_catalog_metadata_identical':True,'cli_source_unchanged':True,'catalog_sha256':m['catalog_sha256'],'verified_at':time.strftime('%Y-%m-%dT%H:%M:%S%z'),'initialize':init}
    write_json(ROOT/'verification.json',evidence); return evidence

def private_dir():
    PRIVATE.mkdir(exist_ok=True)
    if os.name=='nt':
        sid=subprocess.check_output(['whoami','/user','/fo','csv','/nh'],text=True).strip().split(',')[-1].strip('"')
        subprocess.run(['icacls',str(PRIVATE),'/inheritance:r','/grant:r','*'+sid+':(OI)(CI)F'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    else: PRIVATE.chmod(0o700)

class NativeConfig:
    def __init__(self, exe, config):
        env=os.environ.copy(); env['CODEX_HOME']=str(config.parent)
        self.config=config; self.ident=0; self.messages=queue.Queue()
        self.process=subprocess.Popen([str(exe),'app-server','--stdio'],env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,encoding='utf-8')
        def reader():
            for line in self.process.stdout:
                try: self.messages.put(json.loads(line))
                except ValueError: pass
        self.reader=threading.Thread(target=reader,daemon=True); self.reader.start()
        try:
            self.call('initialize',{'clientInfo':{'name':'catalog-repair-config','version':'1'},'capabilities':{'experimentalApi':True}})
            self.process.stdin.write(json.dumps({'method':'initialized','params':{}})+'\n'); self.process.stdin.flush()
        except Exception: self.close(); raise
    def call(self,method,params):
        self.ident+=1
        self.process.stdin.write(json.dumps({'id':self.ident,'method':method,'params':params})+'\n'); self.process.stdin.flush()
        deadline=time.monotonic()+30
        while True:
            if time.monotonic()>=deadline: raise TimeoutError('Native request deadline exceeded')
            value=self.messages.get(timeout=max(.01,deadline-time.monotonic()))
            if value.get('id')==self.ident:
                if 'error' in value:
                    code=value['error'].get('data',{}).get('config_write_error_code','requestRejected')
                    raise ValueError('Native config request rejected: '+code)
                return value['result']
    def read(self):
        result=self.call('config/read',{'includeLayers':True})
        layer=next((x for x in result.get('layers',[]) if x['name']['type']=='user' and Path(x['name']['file']).resolve()==self.config.resolve()),None)
        if not layer: raise ValueError('Cannot identify target user config version')
        return layer['config'],layer['version']
    def write(self,value,version):
        return self.call('config/batchWrite',{'filePath':str(self.config),'expectedVersion':version,'reloadUserConfig':False,'edits':[{'keyPath':KEY,'mergeStrategy':'replace','value':value}]})
    def close(self):
        if self.process.poll() is None:
            if os.name=='nt': subprocess.run(['taskkill','/PID',str(self.process.pid),'/T','/F'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            else: self.process.kill()
        self.process.wait(); self.process.stdin.close(); self.reader.join(timeout=2); self.process.stdout.close()

def apply():
    m=validate_source(); evidence=read_json(ROOT/'verification.json')
    if not evidence.get('passed') or evidence.get('catalog_sha256')!=m['catalog_sha256']: raise ValueError('Current catalog has not passed verify')
    path=str(CATALOG); native=NativeConfig(m['exe'],CONFIG)
    try:
        config,version=native.read()
        if config.get(KEY)==path:
            state=read_json(STATE) if STATE.exists() else {}
            if state.get('config')!=str(CONFIG) or state.get('catalog')!=path or not (state.get('active') or state.get('pending')): raise ValueError('Existing catalog has no active repair ownership')
            state.update(active=True,pending=False); write_json(STATE,state)
            return {'applied':True,'already_applied':True}
        if config.get(KEY) is not None: raise ValueError('Existing user catalog: refusing overwrite')
        raw=CONFIG.read_bytes()
        private_dir()
        if STATE.exists() and (read_json(STATE).get('active') or read_json(STATE).get('pending')): raise ValueError('Previous active or pending repair state exists: restore first')
        backup=PRIVATE/('config-'+str(time.time_ns())+'.toml'); atomic(backup,raw)
        state={'active':False,'pending':True,'config':str(CONFIG),'catalog':path,'backup':str(backup),'before_sha256':digest(raw),'native_expected_version':version}
        write_json(STATE,state)
        # A failed response does not prove the server failed to commit. Keep the
        # durable pending ownership until a fresh read can reconcile the result.
        native.write(path,version)
        state.update(active=True,pending=False); write_json(STATE,state)
        return {'applied':True,'restart_required':True,'native_cas':True}
    finally: native.close()

def status():
    value=parsed(CONFIG.read_bytes()).get(KEY)
    stale=True
    if MANIFEST.exists():
        try: validate_source(); stale=False
        except ValueError: pass
    return {'catalog':value,'source_or_catalog_stale':stale,'repair_state_exists':STATE.exists()}

def restore():
    if not STATE.exists(): return {'restored':False,'reason':'no repair state'}
    state=read_json(STATE)
    if not (state.get('active') or state.get('pending')): return {'restored':False,'reason':'repair already restored'}
    if state['config']!=str(CONFIG): raise ValueError('Config path mismatch')
    native=NativeConfig(active_cli(),CONFIG)
    try:
        config,version=native.read()
        if config.get(KEY)!=state['catalog']:
            state.update(active=False,pending=False); write_json(STATE,state)
            return {'restored':False,'reason':'repair catalog absent or user changed catalog; skipped'}
        native.write(None,version)
        state.update(active=False,pending=False); write_json(STATE,state)
        return {'restored':True,'unrelated_settings_preserved':True,'restart_required':True,'native_cas':True}
    finally: native.close()

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exe',help='Installed Codex CLI executable')
    parser.add_argument('--codex-home',help='Codex configuration directory')
    parser.add_argument('--data-dir',help='Private runtime directory; never commit it')
    parser.add_argument('--remove-model',default='gpt-6-luna')
    parser.add_argument('--fallback-model',default='gpt-6.1-sol')
    parser.add_argument('command',choices=['prepare','verify','apply','status','restore'])
    args=parser.parse_args()
    configure(args.exe,args.codex_home,args.data_dir,args.remove_model,args.fallback_model)
    try:
        if args.command=='status': result=status()
        else:
            from history_titles import locked
            ensure_data_dir()
            with locked(ROOT/'catalog-operation'):
                result=globals()[args.command]()
        print(json.dumps(result,ensure_ascii=True,indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({'stopped':True,'error_type':type(exc).__name__,
                         'message':'Operation stopped; check compatibility, ownership and private local state. No credentials are printed.'}),file=sys.stderr)
        return 1

if __name__=='__main__': sys.exit(main())
