import { useEffect, useRef, useState, type ReactNode } from 'react';
import type { WorkerClient } from '../../lib/useWorker';
import type { Readiness, Route, SessionConfig, SessionSnapshot } from '../../lib/contracts';
const INITIAL: SessionSnapshot = {state:'stopped',counters:{observed:0,matched:0,queued:0,sent:0},attention_reason:null};
export const DEFAULT_CONFIG: SessionConfig = {feed_url:'https://www.threads.com/',mode:'review',continuous:true,duration_seconds:1800,max_posts:100,max_auto_deliveries:10,unlimited_auto_deliveries:true};
const ATTENTION_MESSAGES:Record<string,string>={
  preparation_failed:'Could not prepare the Threads view or local model.',
  browser_preparation_failed:'Threads is not ready. Open its browser view and try again.',
  model_preparation_failed:'The local model could not load. Check setup or reconnect the app.',
  view_hidden:'Collection paused while Threads was hidden. Return to the feed and choose Start collecting to resume.',
  browser_unavailable:'Open the Threads view and keep it visible before checking readiness.',
  inference_unavailable:'The selected local model or runtime is not ready.',
  unknown_layout:'The current feed layout could not be read. In Threads, scroll to another text post and choose Start collecting.',
  manual_login_or_challenge:'Threads is showing a login prompt or platform challenge. Resolve it in the Threads view before collecting.',
  unexpected_navigation:'Threads moved away from the supported home feed.',
  browser_lost:'The Threads view disconnected or closed.',
  browser_ownership_lost:'The app could not confirm access to the current Threads view.',
  snapshot_limit:'The visible feed exceeded the collection size limit.',
  collection_stopped:'Collection ended before the session limit was reached.',
  dependency_cleanup_unconfirmed:'Closing the Threads view or local model is not yet confirmed.',
  browser_shutdown_unconfirmed:'Closing the Threads view is not yet confirmed.',
  sample_runtime_in_use:'A topic sample is still using or closing the local model.',
  duration_limit:'The session reached its time limit.',
  post_limit:'The session reached its post limit.',
  automatic_delivery_limit:'The session reached its automatic delivery limit.',
  system_wake:'The computer woke from sleep. Check readiness before starting again.',
  crash_recovery:'The previous session ended unexpectedly. Check the current state before starting again.',
  telegram_unavailable:'Telegram delivery is not enabled for this mode.',
  automatic_approval_unavailable:'Automatic delivery is not ready. Check the selected route and Telegram connection.',
  automatic_outcome_unknown:'A previous Telegram delivery is unconfirmed. Open Routes → Queue to check it. It will not be sent again automatically.',
  telegram_connection_required:'Connect the saved Telegram account in Accounts, then start collection again.',
  select_one_auto_route:'Choose one enabled Auto route before starting.',
  selected_auto_route_unavailable:'The selected route needs attention. Check its topic, saved chat and account.',
};
function SessionAttention({reason,confirmed}:{reason:string|null;confirmed:boolean}) {
  if(!reason)return null;
  const explanation=Object.hasOwn(ATTENTION_MESSAGES,reason)?ATTENTION_MESSAGES[reason]:null;
  return <div role={['view_hidden','post_limit','duration_limit','automatic_delivery_limit'].includes(reason)?'status':'alert'} className="notice session-notice"><p>{!confirmed&&'Last reported: '}{explanation??'Collection needs attention. Check setup and try again.'}</p><details><summary>Details</summary><code>{reason}</code></details></div>;
}
type Worker = Pick<WorkerClient,'queryState'|'pause'|'stop'> & Partial<Pick<WorkerClient,'initialize'|'queryReadiness'|'start'|'queryRoutes'>>;
export function DashboardView({navigate,worker,onState,initialSnapshot,compact=false,observeInitial=false,refreshKey=0,connectionManaged=false,beforeStart,onStartPending,controlRevision=0,children}: {navigate:(view: 'Accounts' | 'Threads' | 'Topics' | 'Routes')=>void;worker?:Worker;onState?:(value:SessionSnapshot|null)=>void;initialSnapshot?:SessionSnapshot|null;compact?:boolean;observeInitial?:boolean;refreshKey?:number;connectionManaged?:boolean;beforeStart?:()=>Promise<void>;onStartPending?:(pending:boolean)=>void;controlRevision?:number;children?:ReactNode}) {
  const [snapshot,setSnapshot]=useState(initialSnapshot ?? INITIAL),[confirmed,setConfirmedState]=useState(false),[pending,setPending]=useState(false),[error,setError]=useState(''),[cancelling,setCancelling]=useState(false);
  const [config,setConfig]=useState<SessionConfig>(initialSnapshot?.config??DEFAULT_CONFIG),[autoRoutes,setAutoRoutes]=useState<Route[]>([]),[readiness,setReadiness]=useState<Readiness|null>(null);
  const locked=useRef(false),mounted=useRef(true),revision=useRef(0);
  const externalRevision=useRef(controlRevision);if(externalRevision.current!==controlRevision){externalRevision.current=controlRevision;revision.current++;}
  const confirmedNow=useRef(false);
  const setConfirmed=(value:boolean)=>{confirmedNow.current=value;setConfirmedState(value);};
  const initialObservation=useRef(false),observedAny=useRef(initialSnapshot!=null);
  useEffect(()=>{mounted.current=true;return()=>{mounted.current=false;revision.current++;};},[]);
  useEffect(()=>{if(!connectionManaged||locked.current)return;if(initialSnapshot){setSnapshot(initialSnapshot);setConfirmed(true);}else setConfirmed(false);},[connectionManaged,initialSnapshot,pending]);
  useEffect(()=>{if(['starting','running'].includes(snapshot.state)&&snapshot.config)setConfig(snapshot.config);},[snapshot]);
  useEffect(()=>{if(config.mode!=='auto'||!worker?.queryRoutes)return;let active=true;void worker.queryRoutes().then(items=>{if(active)setAutoRoutes(items.filter(route=>route.enabled&&route.mode==='auto'));}).catch(()=>{if(active)setAutoRoutes([]);});return()=>{active=false;};},[worker,refreshKey,config.mode]);
  const update=(next:Partial<SessionConfig>)=>{revision.current++;setReadiness(null);setConfig(previous=>({...previous,...next}));};
  const run=async (operation:'queryState'|'pause'|'stop'|'initialize',initial=false)=>{
    if(!worker||!worker[operation]||locked.current||cancelling)return;
    if(!initial)initialObservation.current=false;
    const started=revision.current;locked.current=true;setPending(true);setError('');
    try {
      const next=await worker[operation]!();
      if(mounted.current&&started===revision.current){
        observedAny.current=true;initialObservation.current=false;
        setSnapshot(next);setConfirmed(operation!=='stop');onState?.(operation==='stop'?null:next);
        if(operation==='stop')setReadiness(null);
      }
    } catch {
      if(mounted.current&&started===revision.current){
        setConfirmed(false);setReadiness(null);onState?.(null);
        if(!initial||observedAny.current){
          initialObservation.current=false;
          setError('The app connection was lost. Use Reconnect app, then choose Start collecting.');
        }
      }
    } finally {locked.current=false;if(mounted.current)setPending(false);}
  };
  useEffect(()=>{
    if(!observeInitial||!worker)return;
    initialObservation.current=!observedAny.current;
    void run('queryState',true);
    // Only the first read waits for background workspace startup. Once any
    // state was observed or the user acted, failures cannot restore authority.
    const timer=setInterval(()=>{if(initialObservation.current)void run('queryState',true);},1000);
    return()=>clearInterval(timer);
  },[observeInitial,worker,refreshKey]);
  useEffect(()=>{if(!confirmed||!['starting','running'].includes(snapshot.state))return;const timer=setInterval(()=>{if(confirmedNow.current)void run('queryState');},1000);return()=>clearInterval(timer);},[confirmed,snapshot.state,worker]);
  const check=async()=>{if(!worker?.queryReadiness||locked.current)return;const started=revision.current;locked.current=true;setPending(true);setError('');setReadiness(null);try{const result=await worker.queryReadiness(config);if(mounted.current&&started===revision.current)setReadiness(result);}catch{if(mounted.current&&started===revision.current){setConfirmed(false);onState?.(null);setError('Readiness could not be checked. No dependency was started.');}}finally{locked.current=false;if(mounted.current)setPending(false);}};
  const start=async()=>{if(!worker?.start||!confirmed||locked.current||(config.mode==='auto'&&!autoRoutes.some(r=>r.id===config.route_ids?.[0])))return;const started=revision.current;locked.current=true;setPending(true);onStartPending?.(true);setError('');setReadiness(null);try{if(beforeStart)await beforeStart();if(!mounted.current||started!==revision.current)return;const next=await worker.start(config);if(mounted.current&&started===revision.current){setSnapshot(next);setConfirmed(true);onState?.(next);}}catch{if(mounted.current&&started===revision.current){setConfirmed(false);onState?.(null);setError('Collection could not start. Check setup for the current requirement, then try again.');}}finally{locked.current=false;if(mounted.current){setPending(false);onStartPending?.(false);}}};
  const cancelPreparation=async()=>{
    if(!worker||cancelling)return;
    initialObservation.current=false;
    const cancelledRevision=++revision.current;
    setCancelling(true);setConfirmed(false);setReadiness(null);onState?.(null);
    setError('Cancellation requested. Waiting for preparation cleanup; no new session authority is confirmed.');
    try{const next=await worker.stop();if(mounted.current&&cancelledRevision===revision.current)setSnapshot(next);}
    catch{if(mounted.current&&cancelledRevision===revision.current)setError('Cancellation requested; cleanup is unconfirmed. Reopen only after the pending operation settles.');}
    finally{if(mounted.current)setCancelling(false);}
  };
  const labels:Record<SessionSnapshot['state'],string>={stopped:'Stopped',starting:'Starting',running:'Running',pausing:'Pausing',paused:'Paused',stopping:'Stopping',needs_attention:'Needs attention',failed:'Failed'};
  const activeConfig=['starting','running'].includes(snapshot.state);
  const modeLabel=snapshot.config&&['starting','running','paused'].includes(snapshot.state)?`${snapshot.config.mode==='dry_run'?'Dry run':snapshot.config.mode==='auto'?'Auto':'Review'}${snapshot.config.mode==='auto'&&snapshot.config.route_ids?.[0]?` · ${snapshot.config.route_ids[0]}`:''}`:null;
  const canStart=(config.mode!=='auto'||autoRoutes.some(r=>r.id===config.route_ids?.[0])) && confirmed && !!worker?.start && !pending && !cancelling && ['stopped','paused','needs_attention','failed'].includes(snapshot.state);
  if(compact)return <><header className={`page-header${connectionManaged?' threads-page-header':''}`}><div><h1 tabIndex={-1}>Threads</h1>{!connectionManaged&&<p>Browse public Threads or sign in, and control collection here.</p>}</div></header>
    <section className={`panel threads-session${connectionManaged?' threads-session-managed':''}`} aria-label="Threads session controls"><p className="threads-session-state" role="status">{labels[snapshot.state]} · {confirmed?'Last validated worker state':'Worker authority unconfirmed'}</p>
      {modeLabel&&<p className="session-mode">Mode: {modeLabel}</p>}
      <SessionAttention reason={snapshot.attention_reason} confirmed={confirmed}/>
      <div className="actions">{!connectionManaged&&<button disabled={!worker?.initialize||pending||cancelling} onClick={()=>void run('initialize')}>Reconnect app</button>}{!connectionManaged&&<><button disabled={!worker||pending} onClick={()=>void run('queryState')}>Refresh session</button><button disabled={!confirmed||!worker?.queryReadiness||pending} onClick={()=>void check()}>Check setup</button></>}<button className="primary" disabled={!canStart} onClick={()=>void start()}>Start collecting</button>{!connectionManaged&&<><button disabled={!confirmed||pending||!['starting','running'].includes(snapshot.state)} onClick={()=>void run('pause')}>Pause</button><button disabled={!worker||cancelling||(!pending&&(!confirmed||!['stopped','starting','running','paused','needs_attention','failed'].includes(snapshot.state)))} onClick={()=>void(pending?cancelPreparation():run('stop'))}>Stop</button></>}</div>
      <details><summary>Collection options · {(config.mode??'review').replaceAll('_',' ')} · {config.continuous?'until paused':`${config.max_posts} posts maximum`}</summary>{connectionManaged&&<><div className="actions"><button disabled={!worker||pending} onClick={()=>void run('queryState')}>Refresh session</button><button disabled={!confirmed||!worker?.queryReadiness||pending} onClick={()=>void check()}>Check setup</button></div><p className="muted">{readiness?readiness.can_start?'Ready for this mode.':`Start unavailable: ${(readiness.reason&&ATTENTION_MESSAGES[readiness.reason])??readiness.reason?.replaceAll('_',' ')??'unconfirmed'}.`:'Choose Start collecting to capture visible posts. Signing in alone does not collect or send.'}</p></>}<p className="muted">Pause collection to change options, then choose Start collecting to use them. Resume collection uses the previous session settings.</p><fieldset disabled={pending||cancelling||activeConfig}><legend>Collection limits and mode</legend>
        <label>Feed URL<input value={config.feed_url} onChange={e=>update({feed_url:e.target.value})}/></label><label>Session mode<select value={config.mode??'review'} onChange={e=>update({mode:e.target.value as SessionConfig['mode'],route_ids:undefined})}><option value="review">Review</option><option value="dry_run">Dry run</option><option value="auto">Auto</option></select></label>
        {config.mode==='auto'&&<><label>Auto route<select value={config.route_ids?.[0]??''} onChange={e=>update({route_ids:e.target.value?[e.target.value]:undefined})}><option value="">Choose an Auto route</option>{autoRoutes.map(route=><option key={route.id} value={route.id}>{route.id}</option>)}</select></label>{config.route_ids?.[0]&&<p className="collection-route-summary">Matched posts use the selected route’s message and queue timing.</p>}</>}<label className="check"><input type="checkbox" checked={config.continuous===true} onChange={e=>update({continuous:e.target.checked})}/>Keep collecting until I pause</label>{!config.continuous&&<><label>Duration in seconds<input type="number" min="1" max="1800" value={config.duration_seconds??1800} onChange={e=>update({duration_seconds:Number(e.target.value)})}/></label><label>Maximum observed posts<input type="number" min="1" max="100" value={config.max_posts??100} onChange={e=>update({max_posts:Number(e.target.value)})}/></label></>}<label className="check"><input type="checkbox" checked={config.unlimited_auto_deliveries===true} onChange={e=>update({unlimited_auto_deliveries:e.target.checked})}/>No automatic delivery limit</label>{!config.unlimited_auto_deliveries&&<label>Maximum automatic deliveries<input type="number" min="0" max="1000000" value={config.max_auto_deliveries??10} onChange={e=>update({max_auto_deliveries:Number(e.target.value)})}/></label>}
      </fieldset></details>
      {error&&<p role="alert" className="error">{error}</p>}{!connectionManaged&&<p className="muted">{readiness?readiness.can_start?'Ready for this mode.':`Start unavailable: ${(readiness.reason&&ATTENTION_MESSAGES[readiness.reason])??readiness.reason?.replaceAll('_',' ')??'unconfirmed'}.`:'Choose Start collecting to capture visible posts. Signing in alone does not collect or send.'}</p>}
    </section>{children}</>;
  return <><header className="page-header"><div><h1 tabIndex={-1}>Dashboard</h1><p>Collect posts in Threads. Review and send them from History.</p></div></header>
    <section className="panel" aria-labelledby="session-heading"><div className="section-heading"><h2 id="session-heading">Collection</h2>{modeLabel&&<span className="session-mode">Mode: {modeLabel}</span>}</div>
      <p className="session-state" role="status">{labels[snapshot.state]}</p>
      <SessionAttention reason={snapshot.attention_reason} confirmed={confirmed}/>
      <p className="muted">{confirmed?'Last validated worker state.':initialSnapshot?'Showing the last known session. Refresh to check its current status.':'Worker not connected; this initial state is unconfirmed.'}</p>{error && <p className="error" role="alert">{error}</p>}
      <div className="actions"><button className="primary" disabled={connectionManaged?pending||cancelling:!canStart} onClick={()=>connectionManaged?navigate('Threads'):void start()}>{connectionManaged?'Collect in Threads':'Start collecting'}</button>{!connectionManaged&&<button disabled={!confirmed||!worker?.queryReadiness||pending} onClick={()=>void check()}>Check setup</button>}{!connectionManaged&&<button disabled={!worker?.initialize||pending||cancelling} onClick={()=>void run('initialize')}>Reconnect app</button>}<button disabled={!worker||pending} onClick={()=>void run('queryState')}>{pending?'Updating…':'Refresh session'}</button>{!connectionManaged&&<><button disabled={!confirmed||pending||!['starting','running'].includes(snapshot.state)} onClick={()=>void run('pause')}>Pause</button><button disabled={!worker||cancelling||(!pending&&(!confirmed||!['stopped','starting','running','paused','needs_attention','failed'].includes(snapshot.state)))} onClick={()=>void (pending?cancelPreparation():run('stop'))}>Stop</button></>}</div>
      {!connectionManaged&&<details><summary>Collection options · {(config.mode??'review').replaceAll('_',' ')} · {config.continuous?'until paused':`${config.max_posts} posts maximum`}</summary><p className="muted">Pause collection to change options, then choose Start collecting to use them. Resume collection uses the previous session settings.</p><fieldset disabled={pending||cancelling||activeConfig}><legend>Collection limits and mode</legend>
        <label>Feed URL<input value={config.feed_url} onChange={e=>update({feed_url:e.target.value})} /></label>
        <label>Session mode<select value={config.mode??'review'} onChange={e=>update({mode:e.target.value as SessionConfig['mode'],route_ids:undefined})}><option value="review">Review</option><option value="dry_run">Dry run</option><option value="auto">Auto</option></select></label>
        {config.mode==='auto'&&<><label>Auto route<select value={config.route_ids?.[0]??''} onChange={e=>update({route_ids:e.target.value?[e.target.value]:undefined})}><option value="">Choose an Auto route</option>{autoRoutes.map(route=><option key={route.id} value={route.id}>{route.id}</option>)}</select></label>{config.route_ids?.[0]&&<p className="collection-route-summary">Matched posts use the selected route’s message and queue timing.</p>}</>}<label className="check"><input type="checkbox" checked={config.continuous===true} onChange={e=>update({continuous:e.target.checked})}/>Keep collecting until I pause</label>{!config.continuous&&<><label>Duration in seconds<input type="number" min="1" max="1800" value={config.duration_seconds??1800} onChange={e=>update({duration_seconds:Number(e.target.value)})} /></label>
        <label>Maximum observed posts<input type="number" min="1" max="100" value={config.max_posts??100} onChange={e=>update({max_posts:Number(e.target.value)})} /></label></>}
        <label className="check"><input type="checkbox" checked={config.unlimited_auto_deliveries===true} onChange={e=>update({unlimited_auto_deliveries:e.target.checked})}/>No automatic delivery limit</label>{!config.unlimited_auto_deliveries&&<label>Maximum automatic deliveries<input type="number" min="0" max="1000000" value={config.max_auto_deliveries??10} onChange={e=>update({max_auto_deliveries:Number(e.target.value)})} /></label>}
      </fieldset></details>}

      {!connectionManaged&&readiness&&<p className="notice">{`Browser: ${readiness.browser} · Model: ${readiness.inference} · Telegram delivery: ${readiness.telegram==='unavailable'?'not enabled':readiness.telegram}. ${readiness.can_start?'Ready for this mode.':`Start unavailable: ${(readiness.reason&&ATTENTION_MESSAGES[readiness.reason])??readiness.reason?.replaceAll('_',' ') ?? 'unconfirmed'}.`}`}</p>}
    </section>
    <dl className="metrics">{(['observed','matched','queued','sent'] as const).map(k => <div className="panel" key={k}><dt>{k[0].toUpperCase()+k.slice(1)}</dt><dd aria-label={`${k[0].toUpperCase()+k.slice(1)} count`}>{snapshot.counters[k]}</dd></div>)}</dl>
    <details><summary>Session details and limits</summary>
      <p>The local model may warm up when the app starts and is used for collection and topic samples. Collection continues across app pages until you pause it.</p>
      <p>{snapshot.config ? `Last validated mode: ${(snapshot.config.mode ?? 'unconfirmed').replaceAll('_',' ')} · feed: ${snapshot.config.feed_url}` : 'Mode and feed are unconfirmed.'}</p>
      <p>Choose continuous collection and no automatic delivery limit to keep running until Pause. Auto sends follow the selected route’s interval.</p><p className="muted">Telegram can require a longer wait. An uncertain send is never retried automatically. Dry run classifies only; no delivery is queued.</p>
      <p className="muted">No new sends start after Pause or Stop; an already transmitted request may still finish.</p>
      <div className="actions"><button onClick={()=>navigate('Topics')}>Topics</button><button onClick={()=>navigate('Accounts')}>Accounts setup</button><button onClick={()=>navigate('Routes')}>Review routes</button></div>
    </details>
  </>;
}
