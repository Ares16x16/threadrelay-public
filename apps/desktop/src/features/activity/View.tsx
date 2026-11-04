import { useState } from 'react';
import {groupEvents} from './groupEvents';
import type { ActivityEntry } from '../../lib/contracts';
import { useActivity, type ActivityWorker } from './useActivity';

const labels:Record<ActivityEntry['kind'],string>={telegram_test_verifying:'Verifying Test group',telegram_test_sending:'Test awaiting Telegram confirmation',telegram_test_sent:'Test confirmed — connection closed',telegram_test_failed:'Telegram test failed',telegram_test_unknown:'Telegram test outcome unknown',session_starting:'Collection starting',session_running:'Collection running',session_paused:'Collection paused',session_attention:'Collection needs attention',session_stopped:'Collection stopped',collection_recovering:'Feed recovery scheduled',collection_recovered:'Feed recovered',reading:'Reading visible feed',skipped:'Post skipped',history_saved:'Saved to History',classification_started:'Model run started',classification_succeeded:'Classification complete',classification_failed:'Classification failed',classification_cancelled:'Classification cancelled',delivery_pending:'Delivery scheduled',delivery_preparing:'Preparing delivery',delivery_sending:'Awaiting Telegram confirmation',delivery_sent:'Sent — confirmed by Telegram',delivery_failed:'Delivery failed',delivery_unknown:'Delivery outcome unknown',delivery_cancelled:'Delivery cancelled',delivery_retryable:'Delivery waiting to retry',job_draft:'Delivery draft saved',job_waiting:'Job scheduled',job_blocked:'Job blocked',job_paused:'Job paused',job_cancelled:'Job cancelled'};
const modelLabels={not_loaded:'Not loaded',starting:'Loading model…',ready:'Loaded',classifying:'Classifying…',stopping:'Closing model…',unavailable:'Unavailable'};
const date=new Intl.DateTimeFormat('en-GB',{timeZone:'Asia/Hong_Kong',day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
function clock(value:string){return date.format(new Date(value));}
function explanation(entry:ActivityEntry):string|null {
 if(entry.kind==='collection_recovered')return 'A supported post is available; collection continues in the same session.';
 if(entry.kind==='collection_recovering'){
  const match=/^(unsupported_cards|unknown_layout|empty_feed):([1-9][0-9]{0,8}):(0|[1-9]|1[0-5])$/.exec(entry.detail??'');
  if(!match)return 'Feed recovery is being checked.';
  const reasons={unsupported_cards:'Unsupported cards were skipped.',unknown_layout:'Waiting for a supported feed layout.',empty_feed:'The feed is empty.'};
  return `${reasons[match[1] as keyof typeof reasons]} Checking again in ${match[3]}s (attempt ${match[2]}).`;
 }
 if(!entry.detail)return null;
 if(entry.detail==='unknown_layout'&&entry.kind==='skipped')return 'Unsupported card skipped.';
 const reasons:Record<string,string>={
  visible_feed:'Checking the visible feed for supported posts.',
  unknown_layout:'The visible feed could not be read safely. Move to another post in Threads, then choose Start collecting.',
  view_hidden:'Collection paused because Threads was hidden. Return to Threads and choose Start collecting.',
  post_limit:'Collection reached its post limit.',
  duration_limit:'Collection reached its time limit.',
  automatic_delivery_limit:'Collection reached its automatic delivery limit.',
  model_unavailable:'The local model was unavailable. Check setup before trying again.',
  inference_unavailable:'The local model was unavailable. Check setup before trying again.',
  application_interrupted:'The app closed before this work finished.',
  session_cancelled:'This work ended when collection was cancelled.',
  cleanup_unconfirmed:'Dependency cleanup has not been confirmed.',
  manual_login_or_challenge:'Resolve the sign-in prompt or platform challenge in Threads before collecting.',
  unexpected_navigation:'Return to the supported Threads home feed before collecting.',
  browser_lost:'The Threads view disconnected or closed.',
  browser_ownership_lost:'Access to the Threads view could not be confirmed.',
  snapshot_limit:'The visible feed exceeded the collection size limit.',
  unsupported_text_layout:'Card skipped because its visible text layout is unsupported.',
  text_clipped:'Card skipped because its visible text was clipped.',
 };
 return reasons[entry.detail]??entry.detail.replaceAll('_',' ');
}
export function ActivityMonitor({worker,compact=false,enabled=true,onOpenHistory}:{worker:ActivityWorker;compact?:boolean;enabled?:boolean;onOpenHistory:()=>void}) {
 const activity=useActivity(worker,enabled),[filter,setFilter]=useState('all');
 const entries=groupEvents(activity.entries.filter(entry=>filter==='all'||entry.category===filter).slice().reverse());
 const recovery=enabled&&activity.following&&!activity.error&&activity.snapshot?.session.state==='running'?activity.snapshot.recovery:null;
 const status=activity.error?'Updates unavailable':activity.snapshot?.recording_ok===false?'Updates incomplete':!enabled?'Observation paused while hidden':!activity.following?'Display frozen':activity.catchingUp?'Catching up…':activity.snapshot?'Following live':'Connecting…';
 return <section className={`panel activity-monitor${compact?' activity-compact':''}`} aria-label={compact?'Threads live activity':'Activity timeline'}>
  <div className="activity-heading"><h2>{compact?'Live activity':'Timeline'}</h2><span className="activity-follow-state" role="status">{status}</span></div>
  <div className="activity-controls"><label>Show activity<select value={filter} onChange={event=>setFilter(event.target.value)}><option value="all">All activity</option><option value="session">Collection state</option><option value="collection">Posts &amp; History</option><option value="model">Model</option><option value="delivery">Delivery</option></select></label><button onClick={activity.following?activity.freeze:activity.follow}>{activity.following?'Freeze display':'Follow live'}</button></div>
  <p className="activity-observed muted">Model: {activity.snapshot?modelLabels[activity.snapshot.model.state]:'Not confirmed'}{activity.snapshot&&<> · Last read <time dateTime={activity.snapshot.as_of}>{clock(activity.snapshot.as_of)}</time> HKT</>}</p>
  {recovery&&<p className="activity-recovery" role="status">Recovering feed · next check in {recovery.retry_in_seconds}s</p>}
  {activity.error&&<div className="activity-read-error"><p role="alert" className="error">{activity.error}</p><button disabled={activity.loading||!enabled} onClick={activity.retry}>Retry activity read</button></div>}
  {activity.snapshot?.recording_ok===false&&<p className="warning" role="alert">Some events could not be recorded. This timeline is incomplete; check History for saved results.</p>}
  <div className="activity-scroll" tabIndex={0} aria-label="Activity events">
   {entries.length===0?<div className="empty"><p>{activity.loading&&!activity.snapshot?'Reading activity…':filter==='all'?'No activity recorded yet.':'No events in this category.'}</p><p>Events are recorded from this app version. Earlier saved posts remain in History.</p></div>:<ol className="activity-events">{entries.map(({entry,occurrences})=><li key={entry.sequence} data-sequence={entry.sequence}>
    <div className="activity-event-heading"><strong>{labels[entry.kind]}</strong><time dateTime={entry.at} title={`${clock(entry.at)} HKT`}>{clock(entry.at)}</time></div>
    {occurrences.length>1&&<details><summary>{occurrences.length} occurrences</summary><p className="muted">{occurrences.map(item=>clock(item.at)).join(' · ')} HKT</p></details>}
    {entry.author&&<p className="activity-author">{entry.author}</p>}
    {entry.text_preview&&<p className="activity-preview">{entry.text_preview}</p>}
    {entry.result&&<span className={`badge${entry.result==='match'?' badge-success':''}`}>{entry.result==='match'?'Match':entry.result==='no_match'?'No match':'Abstained'}</span>}
    {entry.destination&&<p className="muted">To {entry.destination}</p>}
    {entry.due_at&&<p className="muted">Scheduled for <time dateTime={entry.due_at}>{clock(entry.due_at)}</time> HKT</p>}
    {entry.kind==='delivery_preparing'&&<p className="muted">Claimed locally; Telegram has not accepted this delivery.</p>}
    {entry.kind==='delivery_sending'&&<p className="muted">Telegram accepted the request; final delivery is not confirmed.</p>}
    {entry.kind==='delivery_unknown'&&<p className="warning">Check Telegram before any retry. An unknown outcome is not a confirmed failure.</p>}
    {entry.detail&&<p className="activity-detail">{explanation(entry)}</p>}
    {(entry.revision_id||entry.run_id||entry.delivery_id||entry.job_id)&&<button className="text-button" onClick={onOpenHistory}>View in History</button>}
   </li>)}</ol>}
  </div>
  <div className="activity-footer">{!compact&&activity.olderCursor&&<button disabled={activity.loading||!enabled} onClick={activity.loadOlder}>Load older events</button>}<button className="text-button" onClick={onOpenHistory}>Open History</button></div>
  {!compact&&<p className="muted activity-window-note">Newest first · Asia/Hong_Kong · Up to 300 events in this window. Loading older events freezes the display; Follow live returns to recent activity. These controls do not start, stop or reconnect services.</p>}
 </section>;
}
export function ActivityView({worker,onOpenHistory}:{worker:ActivityWorker;onOpenHistory:()=>void}) {
 return <><header className="page-header"><div><h1 tabIndex={-1}>Live activity</h1><p>Follow collection, model work and delivery outcomes. Collection keeps running when you change app pages; use Pause or Stop above to end intake.</p></div></header><ActivityMonitor worker={worker} onOpenHistory={onOpenHistory}/></>;
}
