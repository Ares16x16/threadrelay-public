import { useCallback, useEffect, useRef, useState } from 'react';
import type { DeliveryDraft, DeliveryPreview, DeliveryTiming, MessageTemplate } from '../../lib/contracts';
import { useResource } from '../../lib/useResource';
import { LoadState } from '../common';
import { ReasonList, dueTime } from './Reasons';
import type { DeliveryClient } from './types';

const zone='Asia/Hong_Kong';
const placeholders=['url','author','text','observed_at','published_at','display_name','likes','replies','reposts','quotes','views','shares','topic'];
export function DeliveryComposer({worker,revisionIds,initialDraft,onClose,onApproved}:{worker:DeliveryClient;revisionIds:string[];initialDraft?:DeliveryDraft;onClose:()=>void;onApproved:()=>void}) {
 const load=useCallback(()=>worker.queryDeliveryWorkspace(),[worker]),workspace=useResource(load);
 const loadTopics=useCallback(()=>worker.queryTopics?.()??Promise.resolve([]),[worker]),topics=useResource(loadTopics);
 const [targetIndex,setTargetIndex]=useState(''),[templateId,setTemplateId]=useState('url'),[body,setBody]=useState('{{url}}'),[name,setName]=useState('Custom message'),[nameEdited,setNameEdited]=useState(false),[savedTemplate,setSavedTemplate]=useState<MessageTemplate|null>(null);
 const [mode,setMode]=useState<DeliveryTiming['mode']>(revisionIds.length?'now':'new_matching'),[at,setAt]=useState(''),[interval,setIntervalValue]=useState('5'),[minimum,setMinimum]=useState('5'),[maximum,setMaximum]=useState('15'),[topicIds,setTopicIds]=useState<string[]>([]),[automatic,setAutomatic]=useState(false);
 const loadSources=useCallback(async()=>{
  // The native command queue is bounded. Read one immutable revision at a time
  // so a 50-post selection leaves room for workspace, topic and status reads.
  if(!worker.queryArchiveRevision)throw new Error('Source evidence unavailable');
  const evidence=[];
  for(const revision_id of revisionIds){
   const post=await worker.queryArchiveRevision(revision_id);
   if(!post)throw new Error('Source evidence unavailable');
   evidence.push({revision_id,post_id:post.post_id,visibility:post.visibility,quality_flags:post.quality_flags});
  }
  return evidence;
 },[worker,revisionIds]);
 const sources=useResource(loadSources);
 const [preview,setPreview]=useState<DeliveryPreview|null>(null),[pending,setPending]=useState<'preview'|'approve'|null>(null),[message,setMessage]=useState('');
 const heading=useRef<HTMLHeadingElement>(null);
 const serial=useRef(0),active=useRef(false),locked=useRef(false),draft=useRef<DeliveryDraft|undefined>(initialDraft),restored=useRef(false);
 useEffect(()=>{active.current=true;heading.current?.focus();heading.current?.scrollIntoView?.({block:'start'});return()=>{active.current=false;serial.current++;};},[]);
 const invalidate=()=>{serial.current++;setPreview(null);setMessage('');};
 useEffect(()=>{if(!initialDraft||!workspace.value||restored.current)return;restored.current=true;const d=initialDraft,t=d.timing;setTemplateId(d.template_id);const template=workspace.value.templates.find(item=>item.id===d.template_id&&item.version===d.template_version);if(template){setBody(template.body);setName(template.name);setSavedTemplate(template);}else setMessage('The saved template version is unavailable. Choose a current template and make a new preview.');const target=workspace.value.targets.findIndex(item=>JSON.stringify(item.target)===JSON.stringify(d.target));setTargetIndex(target>=0?String(target):'');setMode(t.mode);if(t.mode==='at_time'){const shifted=new Date(new Date(t.at).getTime()+8*60*60*1000);setAt(shifted.toISOString().slice(0,16));}if(t.mode==='fixed'||t.mode==='new_matching')setIntervalValue(String(t.interval_seconds/60));if(t.mode==='random'){setMinimum(String(t.minimum_seconds/60));setMaximum(String(t.maximum_seconds/60));}if(t.mode==='new_matching'){setTopicIds(t.topic_ids);setAutomatic(t.delivery_mode==='automatic');}},[initialDraft,workspace.value,revisionIds.length]);
 const validSeconds=(value:string)=>{const seconds=Number(value)*60;if(!Number.isInteger(seconds)||seconds<1||seconds>604800)throw new Error('Use an interval between 1 second and 7 days.');return seconds;};
 const timing=():DeliveryTiming=>{
  if(mode==='now')return {mode,timezone:zone};
  if(mode==='at_time'){if(!at)throw new Error('Choose the date and time in Asia/Hong_Kong.');const time=new Date(`${at}+08:00`);if(!Number.isFinite(time.getTime()))throw new Error('Choose a valid date and time.');return {mode,timezone:zone,at:time.toISOString()};}
  if(mode==='fixed')return {mode,timezone:zone,start_at:null,interval_seconds:validSeconds(interval)};
  if(mode==='random'){const min=validSeconds(minimum),max=validSeconds(maximum);if(min>max)throw new Error('The maximum random interval must be at least the minimum.');return {mode,timezone:zone,start_at:null,minimum_seconds:min,maximum_seconds:max};}
  if(!topicIds.length)throw new Error('Choose at least one enabled topic for future matching posts.');
  return {mode,timezone:zone,topic_ids:topicIds as [string,...string[]],interval_seconds:validSeconds(interval),delivery_mode:automatic?'automatic':'review'};
 };
 const prepare=async()=>{
  if(locked.current||!workspace.value||workspace.loading||workspace.error)return;
  invalidate();const ticket=serial.current;locked.current=true;setPending('preview');
  try{
   const schedule=timing();let template=savedTemplate??workspace.value.templates.find(item=>item.id===templateId);
   if(!template||template.body!==body||(nameEdited&&template.name!==name.trim())){template=await worker.saveMessageTemplate({id:template&&!template.readonly?template.id:null,expected_version:template&&!template.readonly?template.version:null,name:name.trim()||'Custom message',body});if(!active.current||serial.current!==ticket)return;setSavedTemplate(template);setTemplateId(template.id);setName(template.name);setNameEdited(false);}
   const selected=targetIndex===''?null:workspace.value.targets[Number(targetIndex)]?.target??null;
   const next=await worker.configureDeliveryDraft({id:draft.current?.id??null,expected_version:draft.current?.version??null,revision_ids:revisionIds,target:selected,template_id:template.id,template_version:template.version,timing:schedule,source_policy:'captured'});
   if(!active.current)return;draft.current=next;if(serial.current!==ticket)return;
   const exact=await worker.previewDeliveryDraft({draft_id:next.id,version:next.version});
   if(active.current&&serial.current===ticket){setPreview(exact);setMessage('Preview ready. Check the recipient and message.');}
  }catch(error){if(active.current&&serial.current===ticket)setMessage(error instanceof Error&&/^Use an interval|^Choose |^The maximum/.test(error.message)?error.message:'Could not prepare this draft. Check the template placeholders, source selection and setup, then try again.');}
  finally{locked.current=false;if(active.current)setPending(null);}
 };
 const approve=async()=>{
  if(locked.current||!preview?.approvable)return;
  const exact=preview;locked.current=true;setPending('approve');setPreview(null);setMessage('');
  try{await worker.approveDeliveryDraft({draft_id:exact.draft_id,version:exact.version,preview_hash:exact.preview_hash});if(active.current){setMessage('Plan saved. Use Send now in Delivery jobs.');onApproved();}}
  catch{if(active.current)setMessage('Plan save was not confirmed. Refresh Delivery jobs before trying again; create a fresh preview if no plan was saved.');}
  finally{locked.current=false;if(active.current)setPending(null);}
 };
 const chooseTemplate=(id:string)=>{invalidate();setTemplateId(id);setSavedTemplate(null);setNameEdited(false);const template=workspace.value?.templates.find(item=>item.id===id);if(template){setBody(template.body);setName(template.readonly?'Custom message':template.name);}};
 return <section className="panel send-composer" aria-label="Prepare send"><div className="section-heading"><h2 tabIndex={-1} ref={heading}>Prepare send</h2><button disabled={pending==='approve'} onClick={onClose}>Close composer</button></div><p>{revisionIds.length} post{revisionIds.length===1?'':'s'} selected. Review the message before sending.</p>
 <LoadState loading={workspace.loading} error={workspace.error} hasValue={false} retry={()=>{invalidate();setTargetIndex('');void workspace.refresh();}}/>
 {workspace.value&&!workspace.loading&&!workspace.error&&<><ReasonList codes={workspace.value.setup_reasons}/><fieldset className="editor-fields" disabled={pending==='approve'}>
 <label>Saved destination<select value={targetIndex} onChange={event=>{invalidate();setTargetIndex(event.target.value);}}><option value="">Choose a saved chat</option>{workspace.value.targets.map(({target,destination},index)=><option key={JSON.stringify(target)} value={index}>{destination.title} · {target.destination_id} · sender {target.sender.kind} {target.sender.id}</option>)}</select></label>

 {!workspace.value.targets.length&&<p className="notice">No saved destination is available for this account and connection. Find, verify and save a chat in Routes, then refresh destinations here.</p>}
 {targetIndex!==''&&workspace.value.targets[Number(targetIndex)]&&<p className="identity">Audience: {workspace.value.targets[Number(targetIndex)].destination.audience} · Effective sender: {workspace.value.targets[Number(targetIndex)].target.sender.kind} {workspace.value.targets[Number(targetIndex)].target.sender.id}</p>}
 <div className="actions"><button disabled={pending!==null} onClick={()=>{invalidate();setTargetIndex('');void workspace.refresh();}}>Refresh destinations</button></div>
 <details open={!revisionIds.length||!!initialDraft&&initialDraft.timing.mode!=='now'}><summary>Message format and timing</summary>
 <label>Saved template<select value={savedTemplate?.id??templateId} onChange={event=>chooseTemplate(event.target.value)}>{workspace.value.templates.map(template=><option key={template.id} value={template.id}>{template.name} · version {template.version}</option>)}{savedTemplate&&!workspace.value.templates.some(t=>t.id===savedTemplate.id)&&<option value={savedTemplate.id}>{savedTemplate.name} · version {savedTemplate.version}</option>}</select></label>
 <label>Template name<input maxLength={120} value={name} onChange={event=>{invalidate();setNameEdited(true);setName(event.target.value);}}/></label><label>Message template<textarea maxLength={4096} value={body} onChange={event=>{invalidate();setBody(event.target.value);}}/></label><p className="muted">Available fields: {placeholders.map(p=>`{{${p}}}`).join(', ')}. Missing fields block sending.</p>
 <label>Timing<select value={mode} onChange={event=>{invalidate();setMode(event.target.value as DeliveryTiming['mode']);}}><option value="now">Send selected records now</option><option value="at_time">Specific date and time</option><option value="fixed">Fixed interval</option><option value="random">Random interval</option><option value="new_matching">New matching history arrivals</option></select></label>
 {mode==='at_time'&&<label>Send at (Asia/Hong_Kong)<input type="datetime-local" value={at} onChange={event=>{invalidate();setAt(event.target.value);}}/></label>}
 {(mode==='fixed'||mode==='new_matching')&&<label>Interval (minutes)<input type="number" min={1/60} max={10080} step="any" value={interval} onChange={event=>{invalidate();setIntervalValue(event.target.value);}}/></label>}
 {mode==='random'&&<div className="details"><label>Minimum interval (minutes)<input type="number" min={1/60} max={10080} step="any" value={minimum} onChange={event=>{invalidate();setMinimum(event.target.value);}}/></label><label>Maximum interval (minutes)<input type="number" min={1/60} max={10080} step="any" value={maximum} onChange={event=>{invalidate();setMaximum(event.target.value);}}/></label></div>}
 <p className="muted">Schedules run while ThreadRelay is open. Restarted jobs need explicit resume.</p></details>
 {mode==='new_matching'&&<><p>Only newly captured matching posts after this rule is approved are enrolled. Selected old posts are examples, not automatically enrolled history.</p><fieldset><legend>Enabled topics for future arrivals</legend><LoadState loading={topics.loading} error={topics.error} hasValue={false} retry={()=>void topics.refresh()}/>{topics.value?.filter(topic=>topic.enabled).map(topic=><label className="check" key={topic.id}><input type="checkbox" checked={topicIds.includes(topic.id)} onChange={event=>{invalidate();setTopicIds(old=>event.target.checked?[...old,topic.id]:old.filter(id=>id!==topic.id));}}/>{topic.description}</label>)}{!topics.loading&&!topics.value?.some(topic=>topic.enabled)&&<p>Save an enabled topic in Topics first.</p>}</fieldset><label className="check"><input type="checkbox" checked={automatic} onChange={event=>{invalidate();setAutomatic(event.target.checked);}}/> Automatically send future matching arrivals using this rule</label><p className="notice">{automatic?'Future matches use this template and saved destination while collection is running.':'Review mode prepares a draft for each future match. Each exact message still needs your approval.'}</p></>}
 <div className="actions"><button className="primary" disabled={pending!==null||!body.trim()||(nameEdited&&!name.trim())||(!revisionIds.length&&mode!=='new_matching')} onClick={()=>void prepare()}>{pending==='preview'?'Preparing preview…':'Save draft and preview'}</button></div></fieldset></>}
 {preview&&<section className="outgoing" aria-label="Exact message preview"><h3>Exact message preview</h3><p>{preview.timing.mode.replaceAll('_',' ')} · {preview.timing.timezone}</p>{preview.target?<p>Destination {preview.target.destination_id} · sender {preview.target.sender.kind} {preview.target.sender.id} · account {preview.target.account_id}</p>:<p>No saved destination selected.</p>}{preview.timing.mode==='fixed'&&<p>Interval: {preview.timing.interval_seconds} seconds</p>}{preview.timing.mode==='random'&&<p>Random interval: {preview.timing.minimum_seconds}–{preview.timing.maximum_seconds} seconds. Each due time below is saved once.</p>}{preview.timing.mode==='new_matching'&&<><p>Topics: {preview.timing.topic_ids.join(', ')} · minimum interval {preview.timing.interval_seconds} seconds · {preview.timing.delivery_mode}</p><p className="preserve">{body}</p></>}{preview.destination&&<p>{preview.destination.title} · {preview.destination.audience} audience · {preview.destination.paid_required?'Paid messaging blocked':'Zero-paid destination snapshot'}</p>}{!!preview.topic_bindings.length&&<><h4>Topic definitions bound to this preview</h4><ul>{preview.topic_bindings.map(topic=><li key={topic.id}>{topic.description} · version {topic.version} <span className="muted">({topic.id})</span></li>)}</ul></>}<ReasonList codes={preview.blocked_reasons}/>{preview.items.map(item=><article key={item.revision_id}><p className="preserve">{item.text}</p><p>Due: {dueTime(item.due_at,preview.timing.timezone)}</p>{sources.value?.filter(source=>source.revision_id===item.revision_id).map(source=><div key={source.revision_id}><p>Recorded source visibility: {source.visibility}</p>{source.quality_flags.length>0&&<ul>{source.quality_flags.map((flag,index)=><li key={index}>{flag.code}: {flag.detail}</li>)}</ul>}</div>)}{!!item.missing_fields.length&&<p className="warning">Missing captured values: {item.missing_fields.join(', ')}</p>}<ReasonList codes={item.blocked_reasons}/></article>)}{preview.timing.mode==='new_matching'&&<p>Future messages use the saved template and chosen destination. New sources are checked when they arrive.</p>}<button className="primary" disabled={!preview.approvable||pending!==null} onClick={()=>void approve()}>Save message plan</button></section>}
 {message&&<p role="status">{message}</p>}</section>;
}
