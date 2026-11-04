import { useCallback, useEffect, useRef, useState } from 'react';
import type { SessionSnapshot } from '../lib/contracts';
import type { WorkerClient } from '../lib/useWorker';
import { useResource } from '../lib/useResource';

const modelLabels={not_loaded:'Not loaded',starting:'Loading…',ready:'Loaded',classifying:'Classifying…',stopping:'Stopping…',unavailable:'Unavailable'};
const sessionLabels={stopped:'Stopped',starting:'Starting…',running:'Running',pausing:'Pausing…',paused:'Paused',stopping:'Stopping…',needs_attention:'Needs attention',failed:'Failed'};
type Worker=Pick<WorkerClient,'queryAppStatus'>&Partial<Pick<WorkerClient,'initialize'|'pause'|'stop'|'suspendCollection'|'stopCollection'>>;
type Operation='pause'|'stop'|'resume'|'reconnect';
export function AppStatusBar({worker,samplePending=false,onReconnect,onState,onResume,onBeforeControl,preparing=false,resumeDisabled=false}:{worker:Worker;samplePending?:boolean;onReconnect?:(snapshot:SessionSnapshot)=>void;onState?:(snapshot:SessionSnapshot|null)=>void;onResume?:()=>Promise<SessionSnapshot|null>;onBeforeControl?:(kind:'pause'|'stop')=>void;preparing?:boolean;resumeDisabled?:boolean}){
 const epoch=useRef(0),pending=useRef<Operation|null>(null),active=useRef(true);
 const readBoundary=useRef(0),lastPreparing=useRef(preparing),failure=useRef<{version:number;boundary:number}|null>(null);
 if(lastPreparing.current!==preparing){lastPreparing.current=preparing;readBoundary.current++;}
 const callbacks=useRef({onReconnect,onState,onResume,onBeforeControl});callbacks.current={onReconnect,onState,onResume,onBeforeControl};
 const load=useCallback(async()=>{const version=pending.current===null?epoch.current:-1,boundary=readBoundary.current;try{return {status:await worker.queryAppStatus(),version,boundary};}catch(error){failure.current={version,boundary};throw error;}},[worker]);
 const data=useResource(load,2500);
 const [operation,setOperation]=useState<Operation|null>(null),[actionError,setActionError]=useState(''),[actionSession,setActionSession]=useState<SessionSnapshot|null>(null);
 useEffect(()=>{active.current=true;return()=>{active.current=false;epoch.current++;};},[]);
 useEffect(()=>{
  if(operation!==null||preparing){if(preparing)setActionSession(null);return;}
  if(data.error){if(failure.current?.version===epoch.current&&failure.current.boundary===readBoundary.current){setActionSession(null);callbacks.current.onState?.(null);}return;}
  if(data.value&&data.value.version===epoch.current&&data.value.boundary===readBoundary.current){setActionSession(null);callbacks.current.onState?.(data.value.status.session);}
 },[data.revision,data.error,operation,preparing]);
 useEffect(()=>{if(!data.error||operation!==null)return;const timer=setInterval(()=>void data.refresh({background:true}),2500);return()=>clearInterval(timer);},[data.error,operation,data.refresh]);
 const control=async(kind:Operation)=>{
  if(pending.current!==null&&(kind!=='stop'||pending.current==='stop'))return;
  const ticket=++epoch.current;pending.current=kind;setOperation(kind);setActionError('');setActionSession(null);
  if(kind==='pause'||kind==='stop')callbacks.current.onBeforeControl?.(kind);
  try {
   const snapshot=kind==='pause'?await (worker.suspendCollection??worker.pause)?.call(worker):kind==='stop'?await (worker.stopCollection??worker.stop)?.call(worker):kind==='resume'?await callbacks.current.onResume?.():await worker.initialize?.();
   if(!snapshot)throw new Error('Control unavailable');
   if(!active.current||ticket!==epoch.current)return;
   setActionSession(snapshot);callbacks.current.onState?.(snapshot);
   if(kind==='reconnect')callbacks.current.onReconnect?.(snapshot);
  } catch {
   if(active.current&&ticket===epoch.current){callbacks.current.onState?.(null);setActionError(kind==='reconnect'?'App connection could not be restored. Close and reopen ThreadRelay.':kind==='resume'?'Collection could not resume. Return to Threads, check its feed, and try Start collecting.':'Collection control is unconfirmed. Use Stop collection and check the app connection.');}
  } finally {
   if(active.current&&ticket===epoch.current){pending.current=null;setOperation(null);await data.refresh();}
  }
 };
 const observed=data.value?.status;
 const available=!!observed&&!data.error&&data.value?.version===epoch.current&&data.value.boundary===readBoundary.current;
 const snapshot=actionSession??(available?observed!.session:null);
 const feedAttention=['unknown_layout','view_hidden','post_limit','duration_limit','automatic_delivery_limit','manual_login_or_challenge','unexpected_navigation','browser_unavailable'];
 const canReconnect=!!data.error||!!(available&&snapshot&&['needs_attention','failed'].includes(snapshot.state)&&!feedAttention.includes(snapshot.attention_reason??''));
 const recovery=available&&snapshot?.state==='running'?observed?.recovery:null;
 const collection=operation==='resume'||preparing?'Starting…':operation==='pause'?'Pausing…':operation==='stop'?'Stopping…':snapshot?sessionLabels[snapshot.state]:'Not confirmed';
 return <div className="global-strip" aria-label="App status">
  <span>App: {operation==='reconnect'?'Connecting…':operation!==null||samplePending?'Working':actionSession?'Ready':data.error?'Unavailable':available?'Ready':'Connecting…'}</span>
  <span title="The local model may warm up at app startup and is used for collection and topic samples.">Model: {samplePending?'Processing sample…':available?modelLabels[observed!.model.state]:'Not confirmed'}</span>
  <span role="status">Collection: {collection}</span>
  {snapshot?.config&&['starting','running','paused'].includes(snapshot.state)&&<span>Mode: {snapshot.config.mode==='dry_run'?'Dry run':snapshot.config.mode==='auto'?'Auto':'Review'}{snapshot.config.mode==='auto'&&snapshot.config.route_ids?.[0]?` · ${snapshot.config.route_ids[0]}`:''}</span>}
  {recovery&&<span className="global-recovery" role="status">Recovering feed · next check in {recovery.retry_in_seconds}s</span>}
  <div className="global-collection-controls" role="group" aria-label="Global collection controls">
   <button disabled={!(worker.suspendCollection??worker.pause)||operation!==null||!snapshot||!['running','starting'].includes(snapshot.state)} onClick={()=>void control('pause')}>Pause collection</button>
   <button disabled={!onResume||resumeDisabled||operation!==null||snapshot?.state!=='paused'} onClick={()=>void control('resume')}>Resume collection</button>
   <button disabled={!(worker.stopCollection??worker.stop)||operation==='stop'} onClick={()=>void control('stop')}>Stop collection</button>
  </div>
  {canReconnect&&<button disabled={!worker.initialize||operation!==null||samplePending||preparing} onClick={()=>void control('reconnect')}>{operation==='reconnect'?'Connecting…':'Reconnect app'}</button>}
  {actionError&&<span role="alert" className="warning">{actionError}</span>}
 </div>;
}
