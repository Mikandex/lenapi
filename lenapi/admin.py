"""Private, database-backed model admin and opt-in process monitoring."""

from __future__ import annotations

import asyncio
import base64
import html
import json
import sys
import threading
import time
from datetime import date, datetime, time as time_value
from decimal import Decimal
from uuid import UUID

from sqlalchemy import Boolean, Date, DateTime, Float, Integer, JSON, Numeric, Time, func
from sqlalchemy import delete, inspect, select, update
from sqlalchemy.orm import object_session
from .models import AdminUser, Model
from .project import project_object_name
from .response import JSONResponse, Response


_INTERNAL_TABLES = {
    project_object_name(name) for name in (
        "admin_user", "lenapi_admin_sessions", "lenapi_security_store",
        "lenapi_background_jobs", "lenapi_social_accounts",
    )
}
_SECRET_COLUMNS = {"password", "api_key", "key_digest", "secret", "client_secret", "token"}


class AdminRegistry:
    """Explicit allow-list and field policy for models exposed in the admin."""

    def __init__(self):
        self._models = {}

    def register(self, model, *, fields=None, readonly=(), exclude=()):
        if not isinstance(model, type):
            raise TypeError("admin.register expects a model class")
        def normalize(value, name, optional=False):
            if optional and value is None:
                return None
            if isinstance(value, str):
                raise TypeError(f"{name} must be an iterable of field names")
            try:
                result = tuple(value)
            except TypeError as exc:
                raise TypeError(f"{name} must be an iterable of field names") from exc
            if not all(isinstance(item, str) for item in result):
                raise TypeError(f"{name} must contain only field names")
            return result
        fields = normalize(fields, "fields", optional=True)
        readonly = normalize(readonly, "readonly")
        exclude = normalize(exclude, "exclude")
        model_name = self.model_name(model)
        if hasattr(model, "model_fields"):
            available = set(model.model_fields)
        else:
            table = getattr(model, "__table__", None)
            available = set(table.columns.keys()) if table is not None else set()
        unknown = (set(fields or ()) | set(readonly) | set(exclude)) - available
        if unknown:
            raise ValueError(
                f"Unknown field(s) for {model.__name__}: {', '.join(sorted(unknown))}"
            )
        selected = None if fields is None else tuple(dict.fromkeys(fields))
        excluded = frozenset(exclude)
        readonly_fields = frozenset(readonly)
        if selected is not None and not readonly_fields.issubset(selected):
            raise ValueError("readonly fields must also be included in fields")
        if selected is not None and excluded.intersection(selected):
            raise ValueError("A field cannot be both included and excluded")
        if readonly_fields.intersection(excluded):
            raise ValueError("A field cannot be both readonly and excluded")
        self._models[model_name] = {
            "model": model,
            "fields": selected,
            "readonly": readonly_fields,
            "exclude": excluded,
        }
        return model

    @staticmethod
    def model_name(model):
        collection_name = getattr(model, "_collection_name", None)
        if callable(collection_name):
            return collection_name()
        table = getattr(model, "__table__", None)
        if table is not None:
            return table.name
        return getattr(model, "__tablename__", model.__name__.lower())

    def get(self, name):
        return self._models.get(name)

    def items(self):
        return self._models.items()


# Application projects import this singleton from ``lenapi.admin`` in
# ``app/admin.py`` and register only the models they intend to expose.
admin = AdminRegistry()


def _json_value(value):
    if isinstance(value, (datetime, date, time_value)):
        return value.isoformat()
    if isinstance(value, (Decimal, UUID)):
        return str(value)
    if isinstance(value, bytes):
        return "[binary]"
    return value


def _model_display(instance, framework_base):
    """Return a model's explicit ``__str__`` label, or its class name.

    MongoDocument inherits Pydantic's generated ``__str__`` representation,
    which includes every field. Stop at the framework base so only a string
    method declared by the application model (or one of its own bases) is used.
    """
    for cls in type(instance).__mro__:
        if cls is framework_base:
            break
        if "__str__" in cls.__dict__:
            try:
                return str(instance)
            except Exception:
                break
    return type(instance).__name__


def _admin_html(title: str, *, login: bool = True, monitor: bool = False) -> str:
    safe_title = html.escape(title)
    monitor_button = '<h2 id="monitor-section" hidden>System</h2><button id="monitor" hidden>Resource monitor</button>' if monitor else ''
    admin_button = '<h2 id="admin-section" hidden>Administrators</h2><button id="admins" hidden>Manage admins</button>'
    dashboard_hidden = "hidden" if login else ""
    dashboard = f'''<div id="admin-dashboard" {dashboard_hidden}><header><div><strong>LenAPI Admin</strong><span id="who"></span></div><button id="logout">Sign out</button></header>
<main><aside><h2>Models</h2><nav id="models"></nav>{monitor_button}{admin_button}</aside>
<section class="workspace" id="model-workspace"><h1 id="heading">Choose a model</h1><p id="notice"></p><div id="actions"></div><div id="table"></div></section>
<section class="workspace monitor-workspace" id="monitor-workspace" hidden><div class="section-heading"><div><h1>Resource monitor</h1><p>Live process and system resource usage.</p></div><span id="monitor-updated">Waiting for metrics…</span></div><div class="metric-cards"><article class="metric-card"><span>CPU</span><strong id="cpu-value">—</strong><canvas id="cpu-chart" aria-label="CPU usage history"></canvas></article><article class="metric-card"><span>RAM · process</span><strong id="ram-process-value">—</strong><small id="ram-system-value">System RAM —</small><canvas id="ram-chart" aria-label="Process memory history"></canvas></article><article class="metric-card process-card"><span>Process</span><strong id="process-value">—</strong><small id="process-name">—</small></article></div><p id="monitor-error" role="status"></p></section>
<section class="workspace" id="admin-workspace" hidden><div class="section-heading"><div><h1>Manage administrators</h1><p>Review administrator accounts, roles, and per-model access.</p></div><button type="button" id="back-to-models">Back to models</button></div><p id="admin-notice" role="status"></p><div id="admin-table"></div><div class="admin-forms"><form id="admin-create-form"><h2>Create administrator</h2><label>Username<input name="username" autocomplete="off" required></label><label>Email<input name="email" type="email" required></label><label>Temporary password<input name="password" type="password" minlength="8" required></label><label>Role<select name="is_superuser"><option value="false">Admin</option><option value="true">Superadmin</option></select></label><fieldset class="permission-matrix"><legend>Model permissions</legend><div id="create-permission-matrix"></div></fieldset><button type="submit">Create administrator</button></form><form id="admin-permissions-form"><h2>Edit administrator</h2><label>Administrator<select name="admin_id" id="managed-admin" required><option value="">Choose an administrator</option></select></label><label>Role<select name="is_superuser" id="managed-role"><option value="false">Admin</option><option value="true">Superadmin</option></select></label><fieldset class="permission-matrix"><legend>Model permissions</legend><div id="edit-permission-matrix"></div></fieldset><button type="submit">Save administrator</button></form></div></section></main>
<dialog id="editor"><form id="record-form"><h2 id="edit-title">Edit record</h2><div id="record-fields"></div><p id="record-error" role="alert"></p><div class="buttons"><button type="button" id="cancel-editor">Cancel</button><button type="submit" id="save">Save</button></div></form></dialog>
 </div>'''
    if login:
        content = f'''<section class="card"><div class="brand">L</div><h1>Admin sign in</h1><p>Sign in to manage application models.</p>
<form id="login"><label>Username<input name="username" autocomplete="username" required></label>
<label>Password<input name="password" type="password" autocomplete="current-password" required></label>
<button>Sign in</button><div id="error" role="alert"></div></form><small>If no admin exists, run <code>python lenora.py admin</code>.</small></section>'''
        content += dashboard
    else:
        content = dashboard
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Admin · {safe_title}</title><style>
*{{box-sizing:border-box}}body{{margin:0;background:#f3f6fb;color:#172033;font:14px Inter,system-ui,sans-serif}}header{{height:64px;background:#101b31;color:white;display:flex;align-items:center;justify-content:space-between;padding:0 24px}}header div{{display:flex;gap:18px;align-items:center}}header span,small{{color:#9aa8bd}}button{{border:0;border-radius:8px;background:#2563eb;color:white;padding:10px 14px;font:inherit;cursor:pointer}}button:hover{{background:#1d4ed8}}button:disabled{{opacity:.78;cursor:wait}}header button{{background:#263753}}.button-spinner{{display:inline-block;width:14px;height:14px;margin-right:8px;vertical-align:-2px;border:2px solid currentColor;border-right-color:transparent;border-radius:50%;animation:button-spin .7s linear infinite}}@keyframes button-spin{{to{{transform:rotate(360deg)}}}}main{{display:grid;grid-template-columns:250px minmax(0,1fr);min-height:calc(100vh - 64px)}}aside{{padding:22px 16px;background:white;border-right:1px solid #e1e7f0}}aside h2{{font-size:12px;text-transform:uppercase;letter-spacing:.09em;color:#718096;margin:18px 8px 10px}}nav button,aside>button{{display:block;width:100%;text-align:left;margin:4px 0;background:transparent;color:#34415a}}nav button:hover,nav button.active,aside>button:hover{{background:#eaf1ff;color:#1744a5}}.workspace{{padding:30px;overflow:auto}}h1{{margin:0 0 20px;font-size:25px}}#actions{{display:flex;gap:10px;margin-bottom:14px}}table{{width:100%;border-collapse:collapse;background:white;border-radius:12px;overflow:hidden}}th,td{{text-align:left;padding:12px;border-bottom:1px solid #e8edf4;max-width:320px;overflow-wrap:anywhere}}th{{background:#f8fafc;color:#536178}}td button{{margin:2px;padding:7px 9px;font-size:12px}}.pagination-controls{{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-top:14px;padding:12px 4px;color:#536178}}.pagination-controls button{{min-width:96px}}.section-heading{{display:flex;align-items:flex-start;justify-content:space-between;gap:16px}}.section-heading p{{color:#64748b}}.metric-cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:18px;margin-top:24px}}.metric-card{{background:white;border:1px solid #e1e7f0;border-radius:16px;padding:20px;box-shadow:0 8px 24px #10234408;min-height:225px}}.metric-card>span{{display:block;color:#64748b;font-weight:650}}.metric-card>strong{{display:block;font-size:30px;margin:10px 0 2px;color:#172033}}.metric-card small{{display:block;color:#64748b}}.metric-card canvas{{display:block;width:100%;height:92px;margin-top:15px}}.process-card{{min-height:0}}#monitor-updated{{color:#64748b}}#monitor-error{{color:#b91c1c}}.admin-forms{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:20px;margin-top:26px}}.admin-forms form{{background:white;border:1px solid #e1e7f0;border-radius:12px;padding:20px}}.admin-forms h2{{margin-top:0}}.permission-matrix{{border:1px solid #dbe3ed;border-radius:8px;padding:12px;margin:18px 0}}.permission-matrix legend{{font-weight:700;padding:0 6px}}.permission-row{{display:grid;grid-template-columns:minmax(100px,1fr) repeat(4,auto);gap:8px;align-items:center;padding:8px 0;border-bottom:1px solid #eef2f7}}.permission-row:last-child{{border-bottom:0}}.permission-row label{{margin:0;font-weight:400;font-size:12px;display:flex;align-items:center;gap:4px}}.permission-row input[type=checkbox]{{width:auto;margin:0}}#admin-notice{{min-height:20px;color:#475569}}.card{{width:min(420px,calc(100% - 32px));margin:12vh auto;background:white;padding:32px;border:1px solid #e3e9f2;border-radius:18px;box-shadow:0 20px 60px #10234418}}.brand{{width:40px;height:40px;display:grid;place-items:center;background:#dbeafe;color:#1d4ed8;border-radius:12px;font-weight:900;font-size:20px}}.card h1{{margin:18px 0 6px}}label{{display:block;margin:18px 0;font-weight:600}}input,textarea,select{{display:block;width:100%;margin-top:8px;border:1px solid #cbd5e1;border-radius:8px;padding:11px;font:inherit}}#record-fields textarea{{min-height:90px}}#record-fields input[type=checkbox]{{width:auto}}#record-fields input[readonly]{{background:#f8fafc;color:#64748b}}.card button{{width:100%}}#error{{color:#b91c1c;margin-top:12px}}dialog{{width:min(620px,calc(100% - 24px));border:0;border-radius:14px;padding:24px;box-shadow:0 20px 70px #0005}}dialog::backdrop{{background:#0f172a88}}textarea{{min-height:260px;font-family:ui-monospace,monospace}}.buttons{{display:flex;justify-content:flex-end;gap:8px;margin-top:12px}}.buttons button:first-child{{background:#64748b}}@media(max-width:720px){{main{{grid-template-columns:1fr}}aside{{border-right:0;border-bottom:1px solid #e1e7f0}}.workspace{{padding:18px}}}}
</style></head><body>{content}<script>
let adminAuthorization='';let base=location.pathname;while(base.endsWith('/'))base=base.slice(0,-1);
async function api(path, opts={{}}){{const headers={{...(opts.headers||{{}})}};if(opts.body)headers['Content-Type']='application/json';if(adminAuthorization)headers.Authorization=adminAuthorization;const r=await fetch(base+'/api'+path,{{...opts,headers,credentials:'omit'}});let data={{}};try{{data=await r.json()}}catch{{}}if(!r.ok)throw new Error(data.detail||'Request failed ('+r.status+')');return data}}
function esc(x){{return String(x??'').replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]))}}
function beginBusy(button,label){{if(!button||button.dataset.busy==='true')return ()=>{{}};const original=button.innerHTML;button.dataset.busy='true';button.disabled=true;button.setAttribute('aria-busy','true');button.innerHTML='<span class="button-spinner" aria-hidden="true"></span>'+esc(label||'Working…');return ()=>{{button.innerHTML=original;button.disabled=false;button.removeAttribute('aria-busy');delete button.dataset.busy}}}}
async function withBusy(button,action,label){{if(button?.dataset.busy==='true')return;const done=beginBusy(button,label);try{{return await action()}}finally{{done()}}}}
async function loadModels(){{const d=await api('/models');document.getElementById('who').textContent=d.identity.username;document.getElementById('models').innerHTML=d.models.map(m=>`<button data-model="${{esc(m.name)}}">${{esc(m.name)}}</button>`).join('');document.querySelectorAll('[data-model]').forEach(b=>b.onclick=()=>withBusy(b,()=>loadTable(b.dataset.model),'Loading…').catch(err=>{{document.getElementById('heading').textContent=b.dataset.model;document.getElementById('notice').textContent=err.message;document.getElementById('actions').innerHTML='';document.getElementById('table').innerHTML=''}}));document.getElementById('notice').textContent=d.models.length?'Select a model to view records.':(d.has_models?'No models are available to this account. Ask a superadmin to grant view access.':'No models found. Check that your models are declared in app/models.py.');}}
async function loadTable(name,offset=0){{clearInterval(window.monitorTimer);document.getElementById('monitor-workspace').hidden=true;document.getElementById('admin-workspace').hidden=true;document.getElementById('model-workspace').hidden=false;window.activeModel=name;document.querySelectorAll('[data-model]').forEach(b=>b.classList.toggle('active',b.dataset.model===name));document.getElementById('heading').textContent=name;const limit=50;const d=await api('/models/'+encodeURIComponent(name)+'?limit='+limit+'&offset='+offset);document.getElementById('notice').textContent=d.permissions.join(' · ');document.getElementById('actions').innerHTML='';const rows=d.rows;const page=d.pagination||{{limit,offset,total_count:rows.length,previous_offset:null,next_offset:null}};if(offset>0&&offset>=page.total_count)return loadTable(name,Math.max(0,offset-limit));window.currentOffset=page.offset;window.formFields=d.fields||[];window.primaryKey=d.primary_key;window.lastRows=Object.fromEntries(rows.map(row=>[String(row[d.primary_key]),row]));const start=page.total_count?offset+1:0;const end=Math.min(offset+rows.length,page.total_count);const columns=d.columns||[];const header='<tr><th>Record</th>'+columns.map(field=>'<th>'+esc(field)+'</th>').join('')+'<th>Actions</th></tr>';const body=rows.map((row,index)=>'<tr><td>'+esc(d.display[index])+'</td>'+columns.map(field=>{{const value=row[field];return '<td>'+esc(value&&typeof value==='object'?JSON.stringify(value):value)+'</td>'}}).join('')+'<td>'+(d.permissions.includes('write')?`<button data-edit="${{esc(row[d.primary_key])}}">Edit</button>`:'')+(d.permissions.includes('delete')?`<button data-delete="${{esc(row[d.primary_key])}}">Delete</button>`:'')+'</td></tr>').join('');document.getElementById('table').innerHTML='<table><thead>'+header+'</thead><tbody>'+body+'</tbody></table>'+'<div class="pagination-controls"><button type="button" id="page-previous"'+(page.previous_offset===null?' disabled':'')+'>Previous</button><span>Showing '+start+'–'+end+' of '+page.total_count+'</span><button type="button" id="page-next"'+(page.next_offset===null?' disabled':'')+'>Next</button></div>';document.getElementById('page-previous').onclick=()=>loadTable(name,page.previous_offset).catch(err=>{{document.getElementById('notice').textContent=err.message}});document.getElementById('page-next').onclick=()=>loadTable(name,page.next_offset).catch(err=>{{document.getElementById('notice').textContent=err.message}});document.querySelectorAll('[data-edit]').forEach(b=>b.onclick=()=>edit(b.dataset.edit));document.querySelectorAll('[data-delete]').forEach(b=>b.onclick=()=>removeRow(b.dataset.delete,b).catch(err=>{{document.getElementById('notice').textContent=err.message}}));}}
function renderField(field,value){{const id='record-field-'+field.name;const label=esc(field.name);const required=field.required?' required':'';const current=value==null?'':value;if(field.readonly)return '<label>'+label+'<input id="'+esc(id)+'" value="'+esc(current)+'" readonly></label>';if(field.kind==='boolean')return '<label>'+label+'<input id="'+esc(id)+'" name="'+label+'" type="checkbox" data-kind="boolean"'+(value?' checked':'')+'></label>';if(field.kind==='json')return '<label>'+label+'<textarea id="'+esc(id)+'" name="'+label+'" data-kind="json"'+required+'>'+esc(current===''?'{{}}':JSON.stringify(value,null,2))+'</textarea></label>';const inputType=field.kind==='integer'||field.kind==='number'?'number':field.kind;return '<label>'+label+'<input id="'+esc(id)+'" name="'+label+'" type="'+esc(inputType)+'" data-kind="'+esc(field.kind)+'"'+(field.max_length?' maxlength="'+field.max_length+'"':'')+(field.step?' step="'+field.step+'"':'')+(field.max!==null?' max="'+esc(field.max)+'"':'')+(field.min!==null?' min="'+esc(field.min)+'"':'')+' value="'+esc(current)+'"'+required+'></label>'}}
function edit(id){{if(!id)return;const dlg=document.getElementById('editor');const record=window.lastRows?.[id];if(!record)return;document.getElementById('edit-title').textContent='Edit record';document.getElementById('record-fields').innerHTML=window.formFields.map(field=>renderField(field,record[field.name])).join('');document.getElementById('record-error').textContent='';dlg.dataset.id=id;dlg.showModal()}}
async function removeRow(id,button){{if(!confirm('Delete this record?'))return;return withBusy(button,async()=>{{await api('/models/'+encodeURIComponent(activeModel)+'/'+encodeURIComponent(id),{{method:'DELETE'}});await loadTable(activeModel,window.currentOffset||0)}},'Deleting…')}}
document.getElementById('cancel-editor')?.addEventListener('click',()=>document.getElementById('editor').close());
document.querySelector('#record-form')?.addEventListener('submit',async e=>{{e.preventDefault();const error=document.getElementById('record-error');const done=beginBusy(e.submitter||document.getElementById('save'),'Saving…');error.textContent='';try{{const value={{}};for(const field of window.formFields){{if(field.readonly)continue;const input=document.getElementById('record-field-'+field.name);if(field.kind==='boolean'){{value[field.name]=input.checked;continue}}const raw=input.value;if(raw===''){{if(field.required)throw new Error(field.name+' is required');if(field.nullable&&!field.has_default)value[field.name]=null;continue}}if(field.kind==='json')value[field.name]=JSON.parse(raw);else if(field.kind==='integer')value[field.name]=Number(raw);else value[field.name]=raw}}const id=document.getElementById('editor').dataset.id;if(!id)throw new Error('Select a record to edit.');delete value[window.primaryKey];await api('/models/'+encodeURIComponent(activeModel)+'/'+encodeURIComponent(id),{{method:'PATCH',body:JSON.stringify(value)}});document.getElementById('editor').close();await loadTable(activeModel,window.currentOffset||0)}}catch(err){{error.textContent=err.message}}finally{{done()}}}});
document.getElementById('logout')?.addEventListener('click',()=>{{adminAuthorization='';location.reload()}});
window.monitorHistory={{cpu:[],ram:[]}};function drawChart(id,values,color){{const canvas=document.getElementById(id);if(!canvas)return;const rect=canvas.getBoundingClientRect();const ratio=window.devicePixelRatio||1;canvas.width=Math.max(1,Math.floor(rect.width*ratio));canvas.height=Math.max(1,Math.floor(rect.height*ratio));const ctx=canvas.getContext('2d');ctx.scale(ratio,ratio);const w=rect.width,h=rect.height;ctx.clearRect(0,0,w,h);ctx.strokeStyle='#e8edf4';ctx.lineWidth=1;for(let i=1;i<=3;i++){{const y=h*i/4;ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(w,y);ctx.stroke()}}if(values.length<2)return;const max=Math.max(...values,1);const min=Math.min(0,...values);ctx.strokeStyle=color;ctx.lineWidth=2.5;ctx.lineJoin='round';ctx.beginPath();values.forEach((value,index)=>{{const x=index*(w/(values.length-1));const y=h-5-((value-min)/(max-min||1))*(h-10);if(index===0)ctx.moveTo(x,y);else ctx.lineTo(x,y)}});ctx.stroke();ctx.lineTo(w,h);ctx.lineTo(0,h);ctx.closePath();ctx.globalAlpha=.1;ctx.fillStyle=color;ctx.fill();ctx.globalAlpha=1}}
async function refreshMonitor(){{try{{const d=await api('/monitor');document.getElementById('cpu-value').textContent=d.cpu_percent+'%';document.getElementById('ram-process-value').textContent=d.process_memory_mb+' MB';document.getElementById('ram-system-value').textContent='System RAM '+d.system_memory_percent+'%';document.getElementById('process-value').textContent='PID '+d.pid+' · '+d.thread_count+' threads';document.getElementById('process-name').textContent=d.process_name||'LenAPI process';document.getElementById('monitor-updated').textContent='Updated '+new Date().toLocaleTimeString();document.getElementById('monitor-error').textContent='';window.monitorHistory.cpu.push(Number(d.cpu_percent)||0);window.monitorHistory.ram.push(Number(d.process_memory_mb)||0);window.monitorHistory.cpu=window.monitorHistory.cpu.slice(-30);window.monitorHistory.ram=window.monitorHistory.ram.slice(-30);drawChart('cpu-chart',window.monitorHistory.cpu,'#2563eb');drawChart('ram-chart',window.monitorHistory.ram,'#7c3aed')}}catch(err){{document.getElementById('monitor-error').textContent=err.message}}}}
document.getElementById('monitor')?.addEventListener('click',e=>withBusy(e.currentTarget,async()=>{{document.getElementById('model-workspace').hidden=true;document.getElementById('admin-workspace').hidden=true;document.getElementById('monitor-workspace').hidden=false;await refreshMonitor();clearInterval(window.monitorTimer);window.monitorTimer=setInterval(refreshMonitor,2000)}},'Loading…').catch(err=>{{document.getElementById('monitor-error').textContent=err.message}}));
function permissionMatrix(containerId,permissions){{const container=document.getElementById(containerId);const models=[...document.querySelectorAll('#models [data-model]')].map(button=>button.dataset.model);const capabilities=['view','read','write','delete'];container.innerHTML=models.map(name=>'<div class="permission-row"><strong>'+esc(name)+'</strong>'+capabilities.map(cap=>'<label><input type="checkbox" data-model="'+esc(name)+'" data-capability="'+cap+'"'+(permissions?.[name]?.[cap]?' checked':'')+'> '+cap+'</label>').join('')+'</div>').join('')||'<p>No application models were discovered.</p>'}}
function readPermissionMatrix(containerId){{const permissions={{}};document.querySelectorAll('#'+containerId+' [data-capability]').forEach(input=>{{const model=input.dataset.model;permissions[model]??={{}};permissions[model][input.dataset.capability]=input.checked}});return permissions}}
async function loadAdmins(){{const notice=document.getElementById('admin-notice');try{{const d=await api('/admins');const select=document.getElementById('managed-admin');select.innerHTML='<option value="">Choose an administrator</option>'+d.admins.map(a=>`<option value="${{esc(a.id)}}">${{esc(a.username)}}${{a.is_superuser?' (superadmin)':''}}</option>`).join('');document.getElementById('admin-table').innerHTML='<table><thead><tr><th>Username</th><th>Email</th><th>Role</th></tr></thead><tbody>'+d.admins.map(a=>'<tr><td>'+esc(a.username)+'</td><td>'+esc(a.email)+'</td><td>'+(a.is_superuser?'Superadmin':'Admin')+'</td></tr>').join('')+'</tbody></table>';notice.textContent=d.admins.length+' administrator(s)';window.adminRows=Object.fromEntries(d.admins.map(a=>[String(a.id),a]));permissionMatrix('create-permission-matrix',{{}});permissionMatrix('edit-permission-matrix',{{}})}}catch(err){{notice.textContent=err.message}}}}
document.getElementById('admins')?.addEventListener('click',e=>withBusy(e.currentTarget,async()=>{{clearInterval(window.monitorTimer);document.getElementById('model-workspace').hidden=true;document.getElementById('monitor-workspace').hidden=true;document.getElementById('admin-workspace').hidden=false;await loadAdmins()}},'Loading…'));
document.getElementById('back-to-models')?.addEventListener('click',()=>{{document.getElementById('admin-workspace').hidden=true;document.getElementById('model-workspace').hidden=false}});
document.getElementById('managed-admin')?.addEventListener('change',e=>{{const admin=window.adminRows?.[e.target.value];document.getElementById('managed-role').value=String(Boolean(admin?.is_superuser));permissionMatrix('edit-permission-matrix',admin?.permissions||{{}})}});
document.getElementById('admin-create-form')?.addEventListener('submit',async e=>{{e.preventDefault();const notice=document.getElementById('admin-notice');const done=beginBusy(e.submitter,'Creating…');try{{const values=Object.fromEntries(new FormData(e.target));values.is_superuser=values.is_superuser==='true';values.permissions=readPermissionMatrix('create-permission-matrix');await api('/admins',{{method:'POST',body:JSON.stringify(values)}});e.target.reset();notice.textContent='Administrator created.';await loadAdmins()}}catch(err){{notice.textContent=err.message}}finally{{done()}}}});
document.getElementById('admin-permissions-form')?.addEventListener('submit',async e=>{{e.preventDefault();const notice=document.getElementById('admin-notice');const done=beginBusy(e.submitter,'Saving…');const adminId=document.getElementById('managed-admin').value;try{{if(!adminId)throw new Error('Choose an administrator first.');const values={{is_superuser:document.getElementById('managed-role').value==='true',permissions:readPermissionMatrix('edit-permission-matrix')}};await api('/admins/'+encodeURIComponent(adminId),{{method:'PATCH',body:JSON.stringify(values)}});notice.textContent='Administrator updated.';await loadAdmins();document.getElementById('managed-admin').value=adminId;document.getElementById('managed-role').value=String(Boolean(window.adminRows[adminId]?.is_superuser));permissionMatrix('edit-permission-matrix',window.adminRows[adminId]?.permissions||{{}})}}catch(err){{notice.textContent=err.message}}finally{{done()}}}});
document.getElementById('login')?.addEventListener('submit',async e=>{{e.preventDefault();const done=beginBusy(e.submitter,'Signing in…');const body=Object.fromEntries(new FormData(e.target));try{{const result=await api('/login',{{method:'POST',body:JSON.stringify(body)}});adminAuthorization='Basic '+btoa(unescape(encodeURIComponent(body.username+':'+body.password)));body.password='';document.querySelector('.card').hidden=true;document.getElementById('admin-dashboard').hidden=false;document.getElementById('who').textContent=result.identity.username;if(result.identity.root){{document.getElementById('admin-section').hidden=false;document.getElementById('admins').hidden=false;if(document.getElementById('monitor-section')){{document.getElementById('monitor-section').hidden=false;document.getElementById('monitor').hidden=false}}}}await loadModels()}}catch(err){{document.getElementById('error').textContent=err.message}}finally{{done()}}}});
document.addEventListener('click',e=>{{const button=e.target.closest('button');if(!button||button.disabled||button.dataset.busy==='true'||(button.form&&button.type==='submit'))return;const done=beginBusy(button,'Working…');setTimeout(done,350)}});
</script></body></html>'''


class AdminConsole:
    """Stateless HTTP Basic admin routes with per-model capability checks."""

    def __init__(self, app, *, path="/admin", monitor=False, cookie_secure=None):
        self.app = app
        self.path = "/" + path.strip("/")
        self.monitor = monitor
        self.cookie_secure = cookie_secure
        self._login_attempts = {}
        self._login_attempts_lock = threading.Lock()

    def _models(self):
        registered = self._registered_models()
        if self.app.mongodb is not None:
            from importlib import import_module
            from .mongodb import MongoDocument

            try:
                module = import_module("app.models")
            except ModuleNotFoundError as exc:
                if exc.name not in {"app", "app.models"}:
                    raise
                module = None
            # Generated projects put models in app/models.py, but applications
            # may split them across app submodules or declare a document class
            # in main.py. Inspect already-loaded application modules as well.
            modules = {}
            if module is not None:
                modules[module.__name__] = module
            for module_name, candidate_module in tuple(sys.modules.items()):
                if candidate_module is None:
                    continue
                if (module_name == "main" or module_name.startswith("app.")):
                    modules[module_name] = candidate_module
            result = {}
            for candidate_module in modules.values():
                for model in vars(candidate_module).values():
                    if (isinstance(model, type) and model is not MongoDocument
                            and issubclass(model, MongoDocument)):
                        model.bind(self.app.mongodb)
                        name = model._collection_name()
                        if name not in _INTERNAL_TABLES and not name.startswith("lenapi_"):
                            result[name] = model
            return self._apply_registration(result, registered)
        result = {}
        for mapper in Model.registry.mappers:
            model = mapper.class_
            table = mapper.local_table
            if table.name not in _INTERNAL_TABLES and not table.name.startswith("lenapi_"):
                result[table.name] = table
        # Explicitly load and inspect the generated project model module too.
        # This makes the admin model list independent of import ordering and
        # covers projects where models are not yet present in the registry
        # when the AdminConsole instance is created.
        from importlib import import_module
        try:
            module = import_module("app.models")
        except ModuleNotFoundError as exc:
            if exc.name not in {"app", "app.models"}:
                raise
            module = None
        if module is not None:
            for candidate in vars(module).values():
                if (not isinstance(candidate, type) or candidate is Model
                        or not issubclass(candidate, Model)):
                    continue
                table = candidate.__table__
                if table.name not in _INTERNAL_TABLES and not table.name.startswith("lenapi_"):
                    result[table.name] = table
        return self._apply_registration(result, registered)

    @staticmethod
    def _registered_models():
        """Load app/admin.py when present; its registry becomes an allow-list."""
        from importlib import import_module
        try:
            module = import_module("app.admin")
        except ModuleNotFoundError as exc:
            if exc.name not in {"app", "app.admin"}:
                raise
            return None
        registry = getattr(module, "admin", None)
        if not isinstance(registry, AdminRegistry):
            raise TypeError("app/admin.py must import `admin` from lenapi.admin")
        return dict(registry.items())

    @staticmethod
    def _apply_registration(discovered, registered):
        # Keep backward compatibility for existing projects that have not
        # created app/admin.py. When it exists, only explicitly registered
        # models are available through this admin console.
        if registered is None:
            return discovered
        result = {}
        for name, options in registered.items():
            if name not in discovered:
                continue
            # The registered class declares the field policy; the actual
            # discovered class/table must match the database selected by this
            # app (SQL and Mongo documents can share a collection name).
            result[name] = discovered[name]
        return result

    def _model_policy(self, name):
        registered = self._registered_models()
        if registered is None:
            return {"fields": None, "readonly": frozenset(), "exclude": frozenset()}
        return registered.get(name, {}).copy() if name in registered else {
            "fields": (), "readonly": frozenset(), "exclude": frozenset()
        }

    def _visible_fields(self, name, spec, is_document):
        policy = self._model_policy(name)
        if is_document:
            available = list(spec.model_fields)
        else:
            available = list(spec.columns.keys())
        requested = policy.get("fields")
        if requested is None:
            selected = available
        else:
            selected = [field for field in requested if field in available]
        excluded = set(policy.get("exclude", ()))
        primary_keys = {"id"} if is_document else {column.name for column in spec.primary_key.columns}
        return [field for field in selected if field not in excluded
                and field not in primary_keys and field.lower() not in _SECRET_COLUMNS]

    def _editable_fields(self, name, spec, is_document):
        readonly = set(self._model_policy(name).get("readonly", ()))
        return set(self._visible_fields(name, spec, is_document)) - readonly

    def _identity(self, authorization):
        if not isinstance(authorization, str) or not authorization.startswith("Basic "):
            return None
        try:
            credentials = base64.b64decode(authorization[6:], validate=True).decode("utf-8")
            username, password = credentials.split(":", 1)
        except (ValueError, UnicodeDecodeError):
            return None
        authenticated = self.app.docs_guard._authenticate(username, password)
        if authenticated is None:
            return None
        if self.app.mongodb is not None:
            admin = self.app.mongodb.collection(project_object_name("admin_user")).find_one({"username": username.strip()})
            if admin is None:
                return None
            permissions = admin.get("permissions", {})
            if isinstance(permissions, str):
                try:
                    permissions = json.loads(permissions)
                except ValueError:
                    permissions = {}
            return {"id": str(admin["_id"]), "username": admin["username"],
                    "root": bool(admin.get("is_superuser", False)),
                    "permissions": permissions if isinstance(permissions, dict) else {}}
        if self.app.database is None:
            return None
        with self.app.database.engine.connect() as conn:
            row = conn.execute(select(AdminUser.id, AdminUser.username, AdminUser.is_superuser,
                                      AdminUser.permissions).where(
                AdminUser.id == int(authenticated["id"]))).first()
        if row is None:
            return None
        try:
            perms = json.loads(row.permissions or "{}")
        except (TypeError, ValueError):
            perms = {}
        return {"id": row.id, "username": row.username, "root": bool(row.is_superuser), "permissions": perms}

    @staticmethod
    def _has(identity, model_name, capability):
        if identity["root"]:
            return True
        permission = identity["permissions"].get(model_name, {})
        return bool(permission.get(capability, False)) if isinstance(permission, dict) else False

    def _response(self, status, detail):
        return JSONResponse({"detail": detail}, status_code=status)

    async def dispatch(self, scope, receive):
        path = scope.get("path", "")
        method = scope.get("method", "GET").upper()
        base = self.path
        if path not in {base, base + "/"} and not path.startswith(base + "/api/" ):
            return None
        if not self.app.admin_enabled:
            return self._response(404, "Not found")
        from .request import Request
        req = Request(scope, receive, max_body_size=self.app.max_request_body_size,
                      body_timeout=self.app.request_body_timeout)
        if path in {base, base + "/"} and method == "GET":
            response = Response(_admin_html(self.app.title, login=True, monitor=self.monitor),
                                media_type="text/html; charset=utf-8")
            return response
        if not path.startswith(base + "/api/"):
            return self._response(405, "Method not allowed")
        endpoint = path[len(base + "/api"):]
        if endpoint == "/login" and method == "POST":
            try:
                data = await req.json()
            except Exception:
                return self._response(400, "Invalid login request")
            client_ip = req.client_ip or "unknown"
            now = time.monotonic()
            with self._login_attempts_lock:
                started, attempts = self._login_attempts.get(client_ip, (now, 0))
                if now - started >= self.app.docs_login_window_seconds:
                    started, attempts = now, 0
                if attempts >= self.app.docs_login_max_attempts:
                    retry_after = max(1, int(self.app.docs_login_window_seconds - (now - started)))
                    return JSONResponse({"detail": "Too many login attempts. Try again later."},
                                        status_code=429, headers={"retry-after": str(retry_after)})
                self._login_attempts[client_ip] = (started, attempts + 1)
            try:
                user = await self.app.docs_guard.authenticate(data.get("username", ""), data.get("password", ""))
            except (RuntimeError, ValueError):
                return self._response(503, "Admin account database is unavailable")
            if user is None:
                return self._response(401, "Invalid username or password")
            with self._login_attempts_lock:
                self._login_attempts.pop(client_ip, None)
            auth_header = "Basic " + base64.b64encode(
                f"{data.get('username', '')}:{data.get('password', '')}".encode("utf-8")
            ).decode("ascii")
            identity = await asyncio.to_thread(self._identity, auth_header)
            if identity is None:
                return self._response(503, "Admin account database is unavailable")
            return JSONResponse({"authenticated": True, "identity": {
                "username": identity["username"], "root": identity["root"]
            }})
        identity = await asyncio.to_thread(self._identity, req.headers.get("authorization", ""))
        if identity is None:
            return self._response(401, "Admin sign in required")
        if endpoint == "/logout" and method == "POST":
            return JSONResponse({"logged_out": True})
        if endpoint == "/models" and method == "GET":
            available_models = sorted(self._models())
            models = [{"name": name} for name in available_models
                      if identity["root"] or self._has(identity, name, "view")]
            return JSONResponse({"identity": {"username": identity["username"]},
                                 "models": models, "has_models": bool(available_models)})
        if endpoint == "/monitor" and method == "GET":
            if not self.monitor:
                return self._response(404, "Resource monitoring is disabled")
            if not identity["root"]:
                return self._response(403, "Superadmin access required")
            try:
                import psutil
            except ImportError:
                return self._response(503, "Install monitoring support with `pip install 'lenapi[monitoring]'`")
            proc = psutil.Process()
            return JSONResponse({
                "pid": proc.pid,
                "process_name": proc.name(),
                "cpu_percent": proc.cpu_percent(interval=None),
                "process_memory_mb": round(proc.memory_info().rss / 1048576, 2),
                "system_memory_percent": psutil.virtual_memory().percent,
                "thread_count": proc.num_threads(),
                "cpu_count": psutil.cpu_count(),
            })
        if endpoint == "/admins" and method == "GET":
            if not identity["root"]:
                return self._response(403, "Superadmin access required")
            if self.app.mongodb is not None:
                def mongo_admins():
                    rows = self.app.mongodb.collection(project_object_name("admin_user")).find({}, {
                        "username": 1, "email": 1, "is_superuser": 1, "permissions": 1,
                    })
                    admins = []
                    for row in rows:
                        permissions = row.get("permissions", {})
                        if isinstance(permissions, str):
                            try:
                                permissions = json.loads(permissions)
                            except ValueError:
                                permissions = {}
                        admins.append({
                            "id": str(row["_id"]), "username": row.get("username"),
                            "email": row.get("email"),
                            "is_superuser": bool(row.get("is_superuser", False)),
                            "permissions": permissions if isinstance(permissions, dict) else {},
                        })
                    return admins
                return JSONResponse({"admins": await asyncio.to_thread(mongo_admins)})
            def sql_admins():
                with self.app.database.engine.connect() as conn:
                    rows = conn.execute(select(AdminUser.id, AdminUser.username, AdminUser.email,
                                               AdminUser.is_superuser, AdminUser.permissions)).all()
                return [{"id": r.id, "username": r.username, "email": r.email,
                         "is_superuser": bool(r.is_superuser),
                         "permissions": json.loads(r.permissions or "{}")} for r in rows]
            admins = await asyncio.to_thread(sql_admins)
            return JSONResponse({"admins": admins})
        if endpoint == "/admins" and method == "POST":
            if not identity["root"]:
                return self._response(403, "Superadmin access required")
            try:
                data = await req.json()
                perms = self._validate_permissions(data.get("permissions", {}))
                is_superuser = data.get("is_superuser", False)
                if not isinstance(is_superuser, bool):
                    return self._response(422, "is_superuser must be a boolean")
                admin_id = await asyncio.to_thread(
                    self.app.docs_guard.create_admin, data["username"], data["email"],
                    data["password"], permissions=perms, is_superuser=is_superuser,
                )
            except (KeyError, TypeError, ValueError) as exc:
                return self._response(422, str(exc) or "Invalid admin data")
            return JSONResponse({"id": admin_id, "created": True}, status_code=201)
        if endpoint.startswith("/admins/") and method == "PATCH":
            if not identity["root"]:
                return self._response(403, "Superadmin access required")
            try:
                admin_id = endpoint.split("/", 2)[2]
                data = await req.json()
                perms = self._validate_permissions(data.get("permissions", {}))
                is_superuser = data.get("is_superuser")
                if is_superuser is not None and not isinstance(is_superuser, bool):
                    return self._response(422, "is_superuser must be a boolean")
                if self.app.mongodb is None:
                    admin_id = int(admin_id)
            except (ValueError, TypeError):
                return self._response(422, "Invalid permissions")
            if str(identity["id"]) == str(admin_id) and is_superuser is False:
                return self._response(409, "You cannot remove your own superadmin access")
            try:
                changed = await asyncio.to_thread(
                    self._update_admin_account, admin_id, perms, is_superuser
                )
            except ValueError as exc:
                return self._response(409, str(exc))
            return JSONResponse({"updated": changed})

        parts = endpoint.strip("/").split("/")
        if len(parts) >= 2 and parts[0] == "models":
            model_name = parts[1]
            spec = self._models().get(model_name)
            if spec is None:
                return self._response(404, "Model not found")
            is_document = self.app.mongodb is not None
            table = None if is_document else spec
            document_model = spec if is_document else None
            if len(parts) == 2 and method == "GET":
                if not (self._has(identity, model_name, "read") or identity["root"]):
                    return self._response(403, "Read access denied")
                if not is_document:
                    missing = await self._missing_table_response(table)
                    if missing is not None:
                        return missing
                try:
                    limit = min(max(int(req.query_params.get("limit", "50")), 1), 100)
                    offset = max(int(req.query_params.get("offset", "0")), 0)
                except ValueError:
                    return self._response(422, "limit and offset must be integers")
                columns, values, pk, display, total_count = await asyncio.to_thread(
                    self._list_model_records, spec, model_name, limit, offset, is_document,
                    self._visible_fields(model_name, spec, is_document),
                )
                column_names = columns if is_document else [c.name for c in columns]
                fields = self._admin_form_fields(
                    spec, is_document, self._visible_fields(model_name, spec, is_document),
                    self._model_policy(model_name).get("readonly", ()),
                )
                return JSONResponse({"model": model_name, "columns": column_names,
                                     "rows": values, "display": display, "fields": fields,
                                     "permissions": [p for p in ("view", "read", "write", "delete")
                                     if self._has(identity, model_name, p)], "primary_key": pk,
                                     "pagination": {
                                         "limit": limit,
                                         "offset": offset,
                                         "total_count": total_count,
                                         "previous_offset": max(0, offset - limit) if offset else None,
                                         "next_offset": offset + limit if offset + limit < total_count else None,
                                     }})
            if len(parts) == 2 and method == "POST":
                return self._response(405, "Creating records through the admin console is disabled")
            if len(parts) == 3:
                try:
                    if is_document:
                        key = parts[2]
                    else:
                        pk = next(iter(table.primary_key.columns))
                        key = self._coerce(pk, parts[2])
                except (ValueError, TypeError):
                    return self._response(422, "Invalid record identifier")
                if method == "PATCH":
                    if not self._has(identity, model_name, "write"):
                        return self._response(403, "Write access denied")
                    if not is_document:
                        missing = await self._missing_table_response(table)
                        if missing is not None:
                            return missing
                    try:
                        data = await req.json()
                        if is_document:
                            data = self._validate_document_values(
                                document_model, data, partial=True,
                                allowed=self._editable_fields(model_name, spec, is_document),
                            )
                        else:
                            data = self._validate_values(
                                table, data,
                                allowed=self._editable_fields(model_name, spec, is_document),
                            )
                        if not data:
                            return self._response(422, "No editable fields supplied")
                        updated = await asyncio.to_thread(
                            self._update_model_record, spec, key, data, is_document
                        )
                        return JSONResponse({"updated": updated})
                    except (ValueError, TypeError, KeyError) as exc:
                        return self._response(422, str(exc))
                if method == "DELETE":
                    if not self._has(identity, model_name, "delete"):
                        return self._response(403, "Delete access denied")
                    if not is_document:
                        missing = await self._missing_table_response(table)
                        if missing is not None:
                            return missing
                    try:
                        deleted = await asyncio.to_thread(
                            self._delete_model_record, spec, key, is_document
                        )
                        return JSONResponse({"deleted": deleted})
                    except Exception:
                        return self._response(409, "Record cannot be deleted because related data protects it")
            return self._response(405, "Method not allowed")
        return self._response(404, "Not found")

    async def _missing_table_response(self, table):
        try:
            exists = await asyncio.to_thread(
                lambda: inspect(self.app.database.engine).has_table(table.name)
            )
        except Exception:
            return self._response(503, "Could not inspect the database schema")
        if exists:
            return None
        return JSONResponse({
            "detail": (
                f"The '{table.name}' table has not been created yet. Run "
                "`python lenora.py migration` and then `python lenora.py migrate`, "
                "then reload this admin page."
            )
        }, status_code=409)

    def _update_admin_account(self, admin_id, permissions, is_superuser):
        if self.app.mongodb is not None:
            from .mongodb import _object_id
            collection = self.app.mongodb.collection(project_object_name("admin_user"))
            target = collection.find_one({"_id": _object_id(admin_id)}, {"is_superuser": 1})
            if target is None:
                return False
            current_role = bool(target.get("is_superuser", False))
            desired_role = current_role if is_superuser is None else is_superuser
            if current_role and not desired_role:
                if collection.count_documents({"is_superuser": True}) <= 1:
                    raise ValueError("The last superadmin cannot be demoted")
            result = collection.update_one(
                {"_id": _object_id(admin_id)},
                {"$set": {"permissions": json.dumps(permissions),
                          "is_superuser": desired_role}},
            )
            return result.modified_count == 1
        with self.app.database.engine.begin() as conn:
            current = conn.execute(select(AdminUser.is_superuser).where(
                AdminUser.id == admin_id
            )).scalar_one_or_none()
            if current is None:
                return False
            desired_role = bool(current) if is_superuser is None else is_superuser
            if current and not desired_role:
                root_count = conn.execute(select(func.count()).select_from(
                    AdminUser.__table__
                ).where(AdminUser.is_superuser.is_(True))).scalar_one()
                if root_count <= 1:
                    raise ValueError("The last superadmin cannot be demoted")
            result = conn.execute(update(AdminUser.__table__).where(
                AdminUser.id == admin_id
            ).values(permissions=json.dumps(permissions), is_superuser=desired_role))
        return result.rowcount == 1

    def _list_model_records(self, spec, model_name, limit, offset, is_document, visible_fields=None):
        if is_document:
            columns = visible_fields if visible_fields is not None else [
                name for name in spec.model_fields if name.lower() not in _SECRET_COLUMNS
            ]
            collection = self.app.mongodb.collection(model_name)
            total_count = collection.count_documents({})
            raw_rows = list(collection.find().skip(offset).limit(limit))
            values = []
            display = []
            from .mongodb import MongoDocument
            for raw in raw_rows:
                record = spec._from_document(raw)
                row = record.model_dump(mode="json", include=set(columns) | {"id"})
                values.append(row)
                if self._model_policy(model_name).get("model") is not None:
                    label = next((row.get(field) for field in columns
                                  if row.get(field) is not None), type(record).__name__)
                    display.append(json.dumps(label, ensure_ascii=False)
                                   if isinstance(label, (dict, list)) else str(label))
                else:
                    display.append(_model_display(record, MongoDocument))
            return columns, values, "id", display, total_count
        table = spec
        visible = set(visible_fields) if visible_fields is not None else None
        columns = [column for column in table.columns
                   if column.name.lower() not in _SECRET_COLUMNS
                   and (visible is None or column.name in visible)]
        model_class = next((mapper.class_ for mapper in Model.registry.mappers
                            if mapper.local_table is table), None)
        if model_class is None:
            raise RuntimeError(f"Could not resolve model class for table {table.name}")
        records = []
        display = []
        primary_column = next(iter(table.primary_key.columns))
        primary_key = primary_column.name
        with self.app.database.session_factory() as session:
            total_count = session.scalar(select(func.count()).select_from(table)) or 0
            records = session.scalars(select(model_class).limit(limit).offset(offset)).all()
            # Evaluate labels while the session is open, allowing __str__ to
            # access ORM relationships such as ``self.course.title``.
            display = [_model_display(record, Model) for record in records]
            values = [{**{column.name: _json_value(getattr(record, column.key))
                        for column in columns},
                       primary_key: _json_value(getattr(record, primary_column.key))}
                      for record in records]
            if self._model_policy(model_name).get("model") is not None:
                display = [str(next((row.get(field) for field in visible_fields
                                     if row.get(field) is not None), type(record).__name__))
                           for record, row in zip(records, values)]
        return columns, values, primary_key, display, total_count

    @staticmethod
    def _admin_form_fields(spec, is_document, visible_fields=None, readonly=()):
        fields = []
        visible = None if visible_fields is None else set(visible_fields)
        readonly = set(readonly)
        if is_document:
            for name, field in spec.model_fields.items():
                if (name == "id" or name.lower() in _SECRET_COLUMNS
                        or (visible is not None and name not in visible)):
                    continue
                annotation = str(field.annotation).lower()
                if "bool" in annotation:
                    kind, step = "boolean", None
                elif "datetime" in annotation:
                    kind, step = "datetime-local", None
                elif "date" in annotation:
                    kind, step = "date", None
                elif "time" in annotation:
                    kind, step = "time", None
                elif "dict" in annotation or "list" in annotation or "any" in annotation:
                    kind, step = "json", None
                elif "int" in annotation:
                    kind, step = "integer", "1"
                elif "float" in annotation or "decimal" in annotation:
                    kind, step = "number", "any"
                else:
                    kind, step = "text", None
                fields.append({"name": name, "kind": kind, "step": step,
                               "readonly": name in readonly,
                               "required": field.is_required(),
                               "has_default": not field.is_required(),
                               "nullable": "none" in annotation,
                               "max_length": getattr(field, "max_length", None),
                               "max": None, "min": None})
            return fields

        primary_keys = {column.name for column in spec.primary_key.columns}
        for column in spec.columns:
            if (column.name in primary_keys or column.name.lower() in _SECRET_COLUMNS
                    or (visible is not None and column.name not in visible)):
                continue
            field_type = column.type
            if isinstance(field_type, Boolean):
                kind, step = "boolean", None
            elif isinstance(field_type, DateTime):
                kind, step = "datetime-local", None
            elif isinstance(field_type, Date):
                kind, step = "date", None
            elif isinstance(field_type, Time):
                kind, step = "time", None
            elif isinstance(field_type, JSON):
                kind, step = "json", None
            elif isinstance(field_type, Integer):
                kind, step = "integer", "1"
            elif isinstance(field_type, Float):
                kind, step = "number", "any"
            elif isinstance(field_type, Numeric):
                kind = "number"
                step = str(10 ** -field_type.scale) if field_type.scale else "1"
            else:
                kind, step = "text", None
            fields.append({"name": column.name, "kind": kind, "step": step,
                           "readonly": column.name in readonly,
                           "required": not column.nullable and column.default is None
                           and column.server_default is None,
                           "has_default": column.default is not None or column.server_default is not None,
                           "nullable": column.nullable,
                           "max_length": getattr(field_type, "length", None),
                           "max": None, "min": None})
        return fields

    def _update_model_record(self, spec, key, values, is_document):
        if is_document:
            record = spec.get(id=key)
            if record is None:
                return False
            spec.model_validate({**record.model_dump(), **values}).save()
            return True
        primary_key = next(iter(spec.primary_key.columns))
        with self.app.database.engine.begin() as conn:
            result = conn.execute(update(spec).where(primary_key == key).values(**values))
        return result.rowcount == 1

    def _delete_model_record(self, spec, key, is_document):
        if is_document:
            record = spec.get(id=key)
            return bool(record.delete()) if record else False
        primary_key = next(iter(spec.primary_key.columns))
        with self.app.database.engine.begin() as conn:
            result = conn.execute(delete(spec).where(primary_key == key))
        return result.rowcount == 1

    @staticmethod
    def _coerce(column, value):
        if value is None:
            return None
        try:
            kind = column.type.python_type
        except (AttributeError, NotImplementedError):
            return value
        if kind is bool:
            if not isinstance(value, bool):
                raise ValueError("Expected a boolean")
            return value
        if kind is datetime:
            return datetime.fromisoformat(value)
        if kind is date:
            return date.fromisoformat(value)
        if kind is time_value:
            return time_value.fromisoformat(value)
        if kind in {int, float, str}:
            if kind is int and isinstance(value, bool):
                raise ValueError("Expected an integer")
            return kind(value)
        if kind is Decimal:
            return Decimal(str(value))
        return value

    @classmethod
    def _validate_values(cls, table, values, *, allowed=None):
        if not isinstance(values, dict):
            raise ValueError("Request body must be a JSON object")
        pk_names = {c.name for c in table.primary_key.columns}
        result = {}
        for name, value in values.items():
            if allowed is not None and name not in allowed:
                raise ValueError(f"Field cannot be edited: {name}")
            if name not in table.columns:
                raise ValueError(f"Unknown field: {name}")
            if name in pk_names or name.lower() in _SECRET_COLUMNS:
                raise ValueError(f"Field cannot be edited: {name}")
            result[name] = cls._coerce(table.c[name], value)
        return result

    @staticmethod
    def _validate_document_values(document_model, values, *, partial=False, allowed=None):
        if not isinstance(values, dict) or (partial and not values):
            raise ValueError("Request body must be a non-empty JSON object")
        for name in values:
            if allowed is not None and name not in allowed:
                raise ValueError(f"Field cannot be edited: {name}")
            if name not in document_model.model_fields or name == "id":
                raise ValueError(f"Unknown or protected field: {name}")
            if name.lower() in _SECRET_COLUMNS:
                raise ValueError(f"Credential field cannot be edited in the admin UI: {name}")
        return values

    @staticmethod
    def _validate_permissions(value):
        if not isinstance(value, dict):
            raise ValueError("permissions must be a model-to-capabilities object")
        result = {}
        allowed = {"view", "read", "write", "delete"}
        for model_name, capabilities in value.items():
            if not isinstance(model_name, str) or not isinstance(capabilities, dict):
                raise ValueError("Each permission entry must map a model name to boolean capabilities")
            unknown = set(capabilities) - allowed
            if unknown:
                raise ValueError("Unknown permission: " + ", ".join(sorted(unknown)))
            if any(not isinstance(flag, bool) for flag in capabilities.values()):
                raise ValueError("Permission values must be booleans")
            result[model_name] = {key: capabilities.get(key, False) for key in sorted(allowed)}
        return result
