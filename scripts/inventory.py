#!/usr/bin/env python3
import argparse, hashlib, json, os, sqlite3, subprocess, sys
from pathlib import Path
from datetime import datetime, timezone

IGNORE={".git",".vs","bin","obj",".code-inventory","node_modules","packages","__pycache__",
        ".venv","venv",".mypy_cache",".pytest_cache","dist","build",".next","coverage",
        ".quasar",".nuxt",".svelte-kit","out","target"}
LANG_BY_EXT={".cs":"cs",".py":"py",".js":"js",".jsx":"js",".mjs":"js",".cjs":"js",".ts":"js",".tsx":"js",".vue":"js"}
LANGS=sorted(set(LANG_BY_EXT.values()))
SCHEMA="""
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS nodes(id INTEGER PRIMARY KEY,parent_id INTEGER REFERENCES nodes(id) ON DELETE CASCADE,kind TEXT,name TEXT,path TEXT,symbol_key TEXT);
CREATE TABLE IF NOT EXISTS file_state(path TEXT PRIMARY KEY,sha256 TEXT NOT NULL,indexed_at TEXT NOT NULL,semantic_status TEXT DEFAULT 'none',language TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS functions(symbol_key TEXT PRIMARY KEY,node_id INTEGER REFERENCES nodes(id) ON DELETE CASCADE,name TEXT,signature TEXT,path TEXT,line INTEGER,description TEXT DEFAULT '',description_origin TEXT DEFAULT 'empty',language TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS calls(id INTEGER PRIMARY KEY,caller_symbol_key TEXT,callee_symbol_key TEXT,callee_name TEXT,path TEXT,line INTEGER,external INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_calls_caller ON calls(caller_symbol_key);
CREATE INDEX IF NOT EXISTS ix_calls_callee ON calls(callee_symbol_key);
CREATE TABLE IF NOT EXISTS sync_runs(id INTEGER PRIMARY KEY,started_at TEXT,git_head TEXT,dirty INTEGER,mode TEXT,changed_files INTEGER,deleted_files INTEGER,analyzer_status TEXT);
CREATE TABLE IF NOT EXISTS diagnostics(id INTEGER PRIMARY KEY,run_id INTEGER,severity TEXT,message TEXT,path TEXT);
"""
SEM_OK="ok"
MIGRATIONS={"file_state":{"semantic_status":"TEXT DEFAULT 'none'","language":"TEXT DEFAULT ''"},
            "functions":{"language":"TEXT DEFAULT ''"}}
def now(): return datetime.now(timezone.utc).isoformat()
def language(rel): return LANG_BY_EXT.get(Path(rel).suffix.lower(),"")
def which_tool(name):
 import shutil
 p=shutil.which(name)
 if p: return p
 if name=="dotnet":
  for cand in (os.environ.get("DOTNET_ROOT"), str(Path.home()/".dotnet")):
   if cand and (Path(cand)/"dotnet").exists(): return str(Path(cand)/"dotnet")
 base=Path.home()/".hermes/tools"
 if base.is_dir():
  for d in sorted(base.glob(name+"-*"),reverse=True):
   cand=d/"bin"/name
   if cand.exists(): return str(cand)
 return None
def db(root):
 p=root/".code-inventory/inventory.sqlite3";p.parent.mkdir(parents=True,exist_ok=True)
 c=sqlite3.connect(p);c.row_factory=sqlite3.Row;c.executescript(SCHEMA)
 for table,cols in MIGRATIONS.items():
  have={r[1] for r in c.execute("PRAGMA table_info("+table+")")}
  for col,spec in cols.items():
   if col not in have: c.execute("ALTER TABLE "+table+" ADD COLUMN "+col+" "+spec)
 return c
def git(root,*a):
 try:
  p=subprocess.run(["git","-C",str(root),*a],capture_output=True,text=True);return p.stdout.strip()
 except FileNotFoundError:return ""
def files(root):
 out=[]
 for base,dirs,names in os.walk(root):
  dirs[:]=[d for d in dirs if d not in IGNORE]
  for n in names:
   p=Path(base)/n
   if p.suffix.lower() in LANG_BY_EXT:out.append(p)
 return sorted(out)
def digest(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def node(c,parent,kind,name,path=None,key=None):
 r=c.execute("SELECT id FROM nodes WHERE parent_id IS ? AND kind=? AND path IS ? AND symbol_key IS ?",(parent,kind,path,key)).fetchone()
 if r:
  c.execute("UPDATE nodes SET name=? WHERE id=?",(name,r[0]));return r[0]
 return c.execute("INSERT INTO nodes(parent_id,kind,name,path,symbol_key) VALUES(?,?,?,?,?)",(parent,kind,name,path,key)).lastrowid
def hierarchy(c,root,rel):
 p=node(c,None,"project",root.name,".",None)
 parts=Path(rel).parts
 for i,part in enumerate(parts[:-1]):p=node(c,p,"folder",part,Path(*parts[:i+1]).as_posix(),None)
 return node(c,p,"file",parts[-1],rel,None)
def diag(c,rid,rel,msg,severity="error"):
 c.execute("INSERT INTO diagnostics(run_id,severity,message,path) VALUES(?,?,?,?)",(rid,severity,msg,rel))
def mark(c,rel,status,sha=None):
 if sha is None:
  c.execute("UPDATE file_state SET semantic_status=? WHERE path=?",(status,rel))
 else:
  c.execute("INSERT OR REPLACE INTO file_state(path,sha256,indexed_at,semantic_status,language) VALUES(?,?,?,?,?)",(rel,sha,now(),status,language(rel)))
def cleanup_calls(c):
 c.execute("DELETE FROM calls WHERE caller_symbol_key IS NOT NULL AND caller_symbol_key NOT IN (SELECT symbol_key FROM nodes WHERE symbol_key IS NOT NULL)")
 c.execute("DELETE FROM calls WHERE external=0 AND callee_symbol_key IS NOT NULL AND callee_symbol_key NOT IN (SELECT symbol_key FROM functions)")
def apply_file(c,rid,rel,fnode,data):
 """Replace one file's semantic rows. Manual descriptions are captured before deletion."""
 lang=language(rel)
 prior={r["symbol_key"]:(r["description"],r["description_origin"]) for r in c.execute("SELECT symbol_key,description,description_origin FROM functions WHERE path=?",(rel,))}
 for s in [r["symbol_key"] for r in c.execute("SELECT symbol_key FROM functions WHERE path=?",(rel,))]:
  c.execute("DELETE FROM calls WHERE caller_symbol_key=?",(s,))
 c.execute("DELETE FROM functions WHERE path=?",(rel,))
 c.execute("DELETE FROM nodes WHERE path=? AND kind NOT IN ('file','folder','project')",(rel,))
 symnodes={}
 for d in data.get("declarations",[]):
  parent=symnodes.get(d.get("parent_symbol_key"),fnode)
  nid=node(c,parent,d.get("kind","member"),d.get("name","?"),rel,d.get("symbol_key"))
  if d.get("kind") in ("method","function","constructor","local_function","component","property") and d.get("symbol_key"):
   key=d["symbol_key"];desc,origin=prior.get(key,("","empty"));symnodes[key]=nid
   c.execute("INSERT OR REPLACE INTO functions(symbol_key,node_id,name,signature,path,line,description,description_origin,language) VALUES(?,?,?,?,?,?,?,?,?)",(key,nid,d.get("name"),d.get("signature"),rel,d.get("line"),desc,origin,lang))
 for x in data.get("calls",[]):
  c.execute("INSERT INTO calls(caller_symbol_key,callee_symbol_key,callee_name,path,line,external) VALUES(?,?,?,?,?,?)",(x.get("caller_symbol_key"),x.get("callee_symbol_key"),x.get("callee_name","?"),rel,x.get("line"),int(bool(x.get("external")))))
 for d in data.get("diagnostics",[]):
  diag(c,rid,rel,d.get("message",""),d.get("severity","warning"))
def analyzer_cmd(lang,root):
 """Project-wide analyzers ship with the skill; C# uses the copy inside the project."""
 skill=Path(__file__).resolve().parents[1]
 if lang=="py":
  p=skill/"analyzers/python/python_analyzer.py"
  return [sys.executable,str(p),"analyze-project","--root",str(root)] if p.exists() else None
 if lang=="js":
  p=skill/"analyzers/js/analyze.js";n=which_tool("node")
  return [n,str(p),"analyze-project","--root",str(root)] if p.exists() and n else None
 return None
def fnode_for(c,rel):
 r=c.execute("SELECT id FROM nodes WHERE kind='file' AND path=?",(rel,)).fetchone()
 return None if r is None else r[0]
def fail_group(c,rid,rels,warnings,msg,hashes,status):
 warnings.append(msg)
 for rel in rels:
  diag(c,rid,rel,msg);mark(c,rel,status,hashes.get(rel) if status=="unavailable" else None)
def run_cs(c,rid,root,rels,hashes,warnings,dn,analyzer):
 for rel in rels:
  fnode=fnode_for(c,rel)
  if fnode is None: warnings.append(rel+": file node missing; skipped");continue
  if not analyzer.exists():
   warnings.append("Analyzer DLL missing; semantic indexing skipped for "+rel)
   diag(c,rid,rel,warnings[-1]);mark(c,rel,"unavailable",hashes[rel]);continue
  if not dn:
   warnings.append("dotnet not found (PATH, DOTNET_ROOT or ~/.dotnet); semantic indexing skipped for "+rel)
   diag(c,rid,rel,warnings[-1]);mark(c,rel,"unavailable",hashes[rel]);continue
  p=subprocess.run([dn,str(analyzer),"analyze-file","--root",str(root),"--file",str(root/rel)],capture_output=True,text=True)
  if p.returncode:
   warnings.append(rel+": analyzer exited "+str(p.returncode)+": "+(p.stderr.strip() or "no stderr")[:300])
   diag(c,rid,rel,warnings[-1]);mark(c,rel,"failed",hashes.get(rel));continue
  try: data=json.loads(p.stdout)
  except Exception as e:
   warnings.append(rel+": invalid analyzer JSON: "+str(e));diag(c,rid,rel,warnings[-1]);mark(c,rel,"failed",hashes.get(rel));continue
  try:
   with c:
    apply_file(c,rid,rel,fnode,data);mark(c,rel,SEM_OK,hashes[rel])
  except Exception as e:
   warnings.append(rel+": apply failed, previous rows kept: "+str(e));diag(c,rid,rel,warnings[-1]);mark(c,rel,"failed",hashes.get(rel))
def run_project(c,rid,root,lang,rels,hashes,warnings):
 argv=analyzer_cmd(lang,root)
 if argv is None:
  fail_group(c,rid,rels,warnings,lang+": no analyzer available (see references/analyzer-contract.md)",hashes,"unavailable");return False
 p=subprocess.run(argv,capture_output=True,text=True)
 if p.returncode:
  fail_group(c,rid,rels,warnings,lang+": analyzer exited "+str(p.returncode)+": "+(p.stderr.strip() or "no stderr")[:300],hashes,"failed");return True
 try: data=json.loads(p.stdout)
 except Exception as e:
  fail_group(c,rid,rels,warnings,lang+": invalid analyzer JSON: "+str(e),hashes,"failed");return True
 entries={}
 for e in data.get("files",[]):
  if isinstance(e,dict) and isinstance(e.get("path"),str): entries[e["path"].replace(os.sep,"/")]=e
 def errors(entry):
  return [d for d in (entry.get("diagnostics") or []) if isinstance(d,dict) and d.get("severity")=="error"]
 for rel in rels:
  if rel in entries: continue
  warnings.append(rel+": analyzer returned no entry");diag(c,rid,rel,warnings[-1]);mark(c,rel,"failed",hashes.get(rel))
 # The analyzer resolves the whole project, so its output for a file that did not change
 # is more current than what the index holds: a symbol this file calls may have been
 # renamed in a file that did change, and applying only `rels` left the old edge pointing
 # at a symbol that no longer exists, which cleanup_calls then deleted.
 for path,e in sorted(entries.items()):
  fnode=fnode_for(c,path)
  if fnode is None: continue
  errs=errors(e)
  try:
   with c:
    apply_file(c,rid,path,fnode,e)
    if path in rels: mark(c,path,"failed" if errs else SEM_OK,hashes.get(path))
  except Exception as ex:
   warnings.append(path+": apply failed, previous rows kept: "+str(ex));diag(c,rid,path,warnings[-1])
   mark(c,path,"failed",hashes.get(path));continue
  if errs and path in rels:
   warnings.append(path+": "+str(errs[0].get("message",""))[:200]);diag(c,rid,path,warnings[-1])
 return True
def sync(a):
 root=Path(a.root).resolve();c=db(root);wanted={a.lang} if getattr(a,"lang",None) else None
 allf=[p for p in files(root) if wanted is None or language(p.relative_to(root).as_posix()) in wanted]
 current={p.relative_to(root).as_posix():p for p in allf}
 old={r["path"]:(r["sha256"],r["semantic_status"]) for r in c.execute("SELECT path,sha256,semantic_status FROM file_state")}
 hashes={k:digest(v) for k,v in current.items()}
 changed=[k for k,v in hashes.items() if a.full or old.get(k,(None,None))[0]!=v]
 retried=[k for k,v in hashes.items() if k not in changed and old.get(k,(None,"none"))[1]!=SEM_OK]
 todo=[k for k in hashes if k in changed or k in retried]
 deleted=sorted(p for p in set(old)-set(current) if wanted is None or language(p) in wanted)
 head=git(root,"rev-parse","HEAD") or None;dirty=bool(git(root,"status","--porcelain"))
 rid=c.execute("INSERT INTO sync_runs(started_at,git_head,dirty,mode,changed_files,deleted_files,analyzer_status) VALUES(?,?,?,?,?,?,?)",(now(),head,int(dirty),"full" if a.full else "incremental",len(changed),len(deleted),"unavailable")).lastrowid
 for rel in deleted:
  c.execute("DELETE FROM nodes WHERE kind='file' AND path=?",(rel,));c.execute("DELETE FROM file_state WHERE path=?",(rel,))
 for rel in current:hierarchy(c,root,rel)
 cleanup_calls(c)
 warnings=[];dn=which_tool("dotnet");dll=root/".code-inventory/analyzer/CodeInventory.Analyzer.dll"
 groups={}
 for rel in todo: groups.setdefault(language(rel),[]).append(rel)
 attempted=False
 for lang,rels in sorted(groups.items()):
  if lang=="cs":
   run_cs(c,rid,root,sorted(rels),hashes,warnings,dn,dll);attempted=True
  elif run_project(c,rid,root,lang,sorted(rels),hashes,warnings): attempted=True
 with c: cleanup_calls(c)
 if not groups: status="ok"
 elif not attempted: status="unavailable"
 else: status="partial" if warnings else "ok"
 c.execute("UPDATE sync_runs SET analyzer_status=? WHERE id=?",(status,rid));c.commit()
 print(json.dumps(dict(root=str(root),git_head=head,working_tree_dirty=dirty,discovered_files=len(allf),changed_files=len(changed),retried_files=len(retried),deleted_files=len(deleted),languages=sorted(groups),analyzer_status=status,warnings=warnings),indent=2))
def main():
 p=argparse.ArgumentParser();s=p.add_subparsers(dest="cmd",required=True)
 for n in ["init","sync","status","search","callers","callees","verify","export"]:
  q=s.add_parser(n);q.add_argument("--root",default=".")
  if n=="sync":
   q.add_argument("--full",action="store_true");q.add_argument("--lang",choices=LANGS)
  if n=="search":
   q.add_argument("--query",required=True);q.add_argument("--lang",choices=LANGS)
  if n in ("callers","callees"):q.add_argument("--symbol",required=True)
  if n=="export":q.add_argument("--output")
 a=p.parse_args();root=Path(a.root).resolve();c=db(root)
 if a.cmd=="init":print("Initialized",root/".code-inventory/inventory.sqlite3")
 elif a.cmd=="sync":sync(a)
 elif a.cmd=="status":
  print(json.dumps({"nodes":{r[0]:r[1] for r in c.execute("SELECT kind,count(*) FROM nodes GROUP BY kind")},"files":{r[0]:r[1] for r in c.execute("SELECT semantic_status,count(*) FROM file_state GROUP BY semantic_status")},"languages":{r[0]:r[1] for r in c.execute("SELECT language,count(*) FROM functions GROUP BY language")},"functions":c.execute("SELECT count(*) FROM functions").fetchone()[0],"calls":c.execute("SELECT count(*) FROM calls").fetchone()[0],"last_sync":dict(c.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()) if c.execute("SELECT count(*) FROM sync_runs").fetchone()[0] else None},indent=2))
 elif a.cmd=="search":
  sql="SELECT * FROM functions WHERE (name LIKE ? OR signature LIKE ? OR description LIKE ?)";args=["%"+a.query+"%"]*3
  if getattr(a,"lang",None): sql+=" AND language=?";args.append(a.lang)
  for r in c.execute(sql,args):print(json.dumps(dict(r)))
 elif a.cmd=="callers":
  for r in c.execute("SELECT c.*,f.name,f.path AS caller_path,f.language FROM calls c LEFT JOIN functions f ON f.symbol_key=c.caller_symbol_key WHERE c.callee_symbol_key=? OR c.callee_name=?",(a.symbol,a.symbol)):print(json.dumps(dict(r)))
 elif a.cmd=="callees":
  for r in c.execute("SELECT * FROM calls WHERE caller_symbol_key=?",(a.symbol,)):print(json.dumps(dict(r)))
 elif a.cmd=="verify":
  orphan=[r[0] for r in c.execute("SELECT f.symbol_key FROM functions f LEFT JOIN nodes n ON n.id=f.node_id WHERE n.id IS NULL")]
  badcaller=[r[0] for r in c.execute("SELECT id FROM calls WHERE caller_symbol_key IS NOT NULL AND caller_symbol_key NOT IN (SELECT symbol_key FROM nodes WHERE symbol_key IS NOT NULL)")]
  badcallee=[r[0] for r in c.execute("SELECT id FROM calls WHERE external=0 AND callee_symbol_key IS NOT NULL AND callee_symbol_key NOT IN (SELECT symbol_key FROM functions)")]
  nosem=[r[0] for r in c.execute("SELECT path FROM file_state WHERE semantic_status IS NOT 'ok' ORDER BY path")]
  ok=not orphan and not badcaller and not badcallee
  print(json.dumps({"ok":ok,"orphan_functions":orphan,"dangling_caller_edges":badcaller,"dangling_callee_edges":badcallee,"files_without_semantics":nosem},indent=2))
  if not ok:raise SystemExit(1)
 elif a.cmd=="export":
  obj={t:[dict(r) for r in c.execute("SELECT * FROM "+t)] for t in ["nodes","functions","calls","file_state"]}
  out=json.dumps(obj,indent=2)
  Path(a.output).write_text(out) if a.output else print(out)
 c.close()
if __name__=="__main__":main()
