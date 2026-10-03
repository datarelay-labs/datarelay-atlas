"""Measurement-only memory/context effectiveness ledger for Verified Memory M5."""
from __future__ import annotations
import json, os
from pathlib import Path
from atlas.data_lock import data_root_write_lock, require_data_root_writer_owner
from atlas.provenance import ValidationError
from atlas.secrets import contains_unsafe_secret

FILENAME="memory-effectiveness.json"
_MAX_ITEMS=4096
_MAX_BYTES=1024*1024

def _load(root: Path):
    p=root/FILENAME
    if not p.exists(): return {"schema_version":1,"kind":"atlas_memory_effectiveness","observations":[]}
    if p.is_symlink() or not p.is_file() or p.stat().st_size>_MAX_BYTES: raise ValidationError("memory effectiveness store is unsafe")
    try: x=json.loads(p.read_text())
    except OSError as e: raise ValidationError("memory effectiveness store is unreadable") from e
    except (ValueError,UnicodeError) as e: raise ValidationError("memory effectiveness store is invalid") from e
    if not isinstance(x,dict) or set(x)!={"schema_version","kind","observations"} or x["schema_version"]!=1 or x["kind"]!="atlas_memory_effectiveness" or not isinstance(x["observations"],list) or len(x["observations"])>_MAX_ITEMS:
        raise ValidationError("memory effectiveness store is invalid")
    return x

def record_effectiveness(root: Path, observation: dict[str,object]):
    allowed={"project_id","repository","workstream","observed_at","important_expected","important_recalled","stale_injected","irrelevant_injected","duplicate_injected","injected_context_bytes","repeated_owner_explanations","first_pass_success"}
    if not isinstance(observation,dict) or set(observation)!=allowed: raise ValidationError("memory effectiveness observation is invalid")
    for k in ("project_id","repository","workstream","observed_at"):
        v=observation[k]
        if not isinstance(v,str) or not v or len(v)>128 or contains_unsafe_secret(v): raise ValidationError("memory effectiveness observation is invalid")
    for k in ("important_expected","important_recalled","stale_injected","irrelevant_injected","duplicate_injected","injected_context_bytes","repeated_owner_explanations"):
        v=observation[k]
        if type(v) is not int or v<0: raise ValidationError("memory effectiveness observation is invalid")
    if observation["important_recalled"]>observation["important_expected"] or not isinstance(observation["first_pass_success"],bool):
        raise ValidationError("memory effectiveness observation is invalid")
    root=Path(root); require_data_root_writer_owner(root); p=root/FILENAME
    with data_root_write_lock(root):
        store=_load(root); items=[*store["observations"],dict(observation)]
        if len(items)>_MAX_ITEMS: raise ValidationError("memory effectiveness store is full")
        raw=(json.dumps({"schema_version":1,"kind":"atlas_memory_effectiveness","observations":items},indent=2,sort_keys=True)+"\n").encode()
        if len(raw)>_MAX_BYTES or contains_unsafe_secret(raw.decode()): raise ValidationError("memory effectiveness store exceeds bounds")
        tmp=p.with_suffix(".json.tmp")
        if tmp.exists() or tmp.is_symlink() or p.is_symlink(): raise ValidationError("memory effectiveness store is unsafe")
        with tmp.open("xb") as f: f.write(raw); f.flush(); os.fsync(f.fileno())
        os.replace(tmp,p); os.chmod(p,0o600)
    return {"state":"RECORDED","observation_count":len(items),"policy_mutated":False}

def effectiveness_report(root: Path, *, project_id: str|None=None):
    items=_load(Path(root))["observations"]
    if project_id is not None: items=[x for x in items if x["project_id"]==project_id]
    n=len(items)
    if n==0: return {"state":"NO_DATA","observation_count":0,"policy_mutated":False,"metrics":None}
    expected=sum(x["important_expected"] for x in items); recalled=sum(x["important_recalled"] for x in items)
    injected=sum(x["important_recalled"]+x["stale_injected"]+x["irrelevant_injected"]+x["duplicate_injected"] for x in items)
    metrics={
      "important_memory_recall_rate": (recalled/expected if expected else None),
      "stale_injection_rate": (sum(x["stale_injected"] for x in items)/injected if injected else 0.0),
      "irrelevant_injection_rate": (sum(x["irrelevant_injected"] for x in items)/injected if injected else 0.0),
      "duplicate_injection_rate": (sum(x["duplicate_injected"] for x in items)/injected if injected else 0.0),
      "repeated_owner_explanation_count": sum(x["repeated_owner_explanations"] for x in items),
      "average_injected_context_bytes": sum(x["injected_context_bytes"] for x in items)/n,
      "first_pass_success_rate": sum(1 for x in items if x["first_pass_success"])/n,
    }
    return {"state":"MEASURED" if n>=10 else "INSUFFICIENT","observation_count":n,"minimum_measured_observations":10,"policy_mutated":False,"metrics":metrics}
