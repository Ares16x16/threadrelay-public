import { useEffect, useRef, useState } from 'react';
import type { DeliveryJob, ManualHistoryPreview, ManualHistorySendState } from '../../lib/contracts';
import { ManualHistoryPrepareError, type WorkerClient } from '../../lib/useWorker';
import { ConfirmDialog } from '../../components/ConfirmDialog';

export type ManualHistoryClient=Pick<WorkerClient,'previewManualHistorySend'|'sendManualHistoryOnce'|'queryManualHistorySend'>;
export type ManualHistoryOutcome='sent'|'failed'|'unknown';
export function canSendHistoryNow(job:DeliveryJob) {
 const target=job.target;
 return job.kind==='job'&&job.rule_id===null&&job.timing.mode==='now'&&!job.requires_approval&&['blocked','waiting'].includes(job.status)&&target?.sender.kind==='user'&&target.sender.id===target.account_id&&/^-[1-9][0-9]*$/.test(target.destination_id);
}
const unknownMessage='Outcome unknown. Do not resend. Check Telegram and reconnect in Accounts.';
export const manualHistoryOutcomeMessage=(state:ManualHistoryOutcome)=>state==='sent'?'Message sent.':state==='failed'?'Send failed. Check Telegram and reconnect in Accounts before trying again.':unknownMessage;
function matches(preview:ManualHistoryPreview,job:DeliveryJob) {
 const target=job.target;
 return !!target&&preview.job_id===job.id&&preview.job_version===job.version&&preview.revision_id===job.revision_id&&preview.text===job.text&&
  preview.target.account_id===target.account_id&&preview.target.account_epoch===target.account_epoch&&preview.target.destination_id===target.destination_id&&preview.target.destination_version===target.destination_version&&preview.target.sender.kind===target.sender.kind&&preview.target.sender.id===target.sender.id&&
  preview.target.sender.kind==='user'&&preview.target.sender.id===preview.target.account_id&&preview.destination.account_id===preview.target.account_id&&preview.destination.chat_id===preview.target.destination_id&&preview.destination.sender.kind===preview.target.sender.kind&&preview.destination.sender.id===preview.target.sender.id&&!preview.destination.paid_required&&preview.destination.permissions.can_send&&Date.parse(preview.expires_at)>Date.now();
}
export function ManualHistorySend({worker,job,disabled=false,onBusy,onTerminal}:{worker:ManualHistoryClient;job:DeliveryJob;disabled?:boolean;onBusy?:(busy:boolean)=>void;onTerminal?:(state:ManualHistoryOutcome)=>void|Promise<unknown>}) {
 const [preview,setPreview]=useState<ManualHistoryPreview|null>(null),[phase,setPhase]=useState<'idle'|'previewing'|'confirming'|'sending'|'terminal'>('idle'),[message,setMessage]=useState('');
 const generation=useRef(0),mounted=useRef(false),lock=useRef(false),dispatched=useRef(false),terminal=useRef(false),timer=useRef<ReturnType<typeof setTimeout>|undefined>(undefined);
 const callbacks=useRef({onBusy,onTerminal});callbacks.current={onBusy,onTerminal};
 const basis=JSON.stringify([job.id,job.version,job.revision_id,job.text,job.target,job.status,job.requires_approval,job.rule_id,job.timing]);
 useEffect(()=>{mounted.current=true;generation.current++;setPreview(null);if(dispatched.current){setPhase('terminal');setMessage(unknownMessage);lock.current=true;}else{setPhase('idle');setMessage('');lock.current=false;}callbacks.current.onBusy?.(false);return()=>{mounted.current=false;generation.current++;if(timer.current)clearTimeout(timer.current);callbacks.current.onBusy?.(false);};},[worker,basis]);
 const live=(ticket:number)=>mounted.current&&ticket===generation.current;
 const finish=(ticket:number,state:ManualHistoryOutcome)=>{if(!live(ticket)||terminal.current)return;terminal.current=true;setPhase('terminal');setMessage(manualHistoryOutcomeMessage(state));callbacks.current.onBusy?.(false);void Promise.resolve(callbacks.current.onTerminal?.(state)).catch(()=>{});};
 const observe=async(ticket:number):Promise<void>=>{
  try{const value:ManualHistorySendState=await worker.queryManualHistorySend();if(!live(ticket))return;
   if(value.job_id!==job.id||value.job_version!==job.version||value.state==='idle'){finish(ticket,'unknown');return;}
   if(value.state==='sent'||value.state==='failed'||value.state==='unknown'){finish(ticket,value.state);return;}
   setMessage(value.state==='verifying'?'Checking this chat…':value.state==='sending'?'Sending one message…':'Closing Telegram…');
   timer.current=setTimeout(()=>void observe(ticket),500);
  }catch{finish(ticket,'unknown');}
 };
 const prepare=async()=>{
  if(disabled||lock.current||!canSendHistoryNow(job))return;lock.current=true;const ticket=++generation.current;setPhase('previewing');setMessage('Preparing message…');callbacks.current.onBusy?.(true);
  try{const value=await worker.previewManualHistorySend({job_id:job.id,version:job.version});if(!live(ticket))return;if(!matches(value,job))throw new Error('Preview changed');setPreview(value);setPhase('confirming');setMessage('');timer.current=setTimeout(()=>{if(live(ticket)&&!dispatched.current){setPreview(null);setPhase('idle');setMessage('Preview expired. Choose Send now for a fresh preview.');lock.current=false;callbacks.current.onBusy?.(false);}},Math.max(0,Date.parse(value.expires_at)-Date.now()));}
  catch(error){if(live(ticket)){setPhase('idle');setMessage(error instanceof ManualHistoryPrepareError?error.message:'Could not prepare this message. Refresh jobs and check Accounts.');lock.current=false;callbacks.current.onBusy?.(false);}}
 };
 const cancel=()=>{if(dispatched.current)return;generation.current++;if(timer.current)clearTimeout(timer.current);setPreview(null);setPhase('idle');lock.current=false;callbacks.current.onBusy?.(false);};
 const send=async()=>{
  if(disabled||!preview||dispatched.current||phase!=='confirming'||!matches(preview,job))return;
  const value=preview,ticket=generation.current;dispatched.current=true;if(timer.current)clearTimeout(timer.current);setPreview(null);setPhase('sending');setMessage('Checking this chat…');
  try{await worker.sendManualHistoryOnce({job_id:value.job_id,version:value.job_version,preview_receipt:value.preview_receipt});}catch{/* Read status even when the acknowledgment is uncertain. */}
  if(live(ticket))await observe(ticket);
 };
 if(!canSendHistoryNow(job))return null;
 return <div className="manual-send-control">{phase!=='terminal'&&phase!=='sending'&&<button type="button" className="primary" disabled={disabled||phase!=='idle'} onClick={()=>void prepare()}>{phase==='previewing'?'Preparing…':'Send now'}</button>}{message&&<p role="status">{message}</p>}{preview&&<ConfirmDialog title="Send this message?" confirmLabel="Send this message" disabled={disabled} onCancel={cancel} onConfirm={()=>void send()}><p><strong>{preview.destination.title||'Unnamed chat'} · {preview.destination.chat_id}</strong><br/>{preview.destination.audience} audience · Sender {preview.target.sender.kind} · {preview.target.sender.id}</p><p className="preserve outgoing">{preview.text}</p>{disabled&&<p role="status">Connection changed. Cancel and check Accounts.</p>}<p>One message. Your Telegram login stays connected.</p><details><summary>Delivery details</summary><p>Job {preview.job_id} · version {preview.job_version} · revision {preview.revision_id}</p><p>Account {preview.target.account_id} · epoch {preview.target.account_epoch}</p></details></ConfirmDialog>}</div>;
}
