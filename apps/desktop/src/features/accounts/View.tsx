import { useCallback, useEffect, useRef, useState } from 'react';
import type { AccountView, AuthChallenge, LogoutRequest } from '../../lib/contracts';
import type { WorkerClient } from '../../lib/useWorker';
import { TelegramLoginBlockedError, TelegramLoginProblemError } from '../../lib/useWorker';
import { useResource } from '../../lib/useResource';
import { SecureInput } from '../../components/SecureInput';
import { SecureQr } from '../../components/SecureQr';
import { SecureCredentials } from '../../components/SecureCredentials';
import { ConfirmDialog } from '../../components/ConfirmDialog';
import { LoadState } from '../common';
import { LoginCodeDelivery } from './LoginCodeDelivery';
import type { SecureStatus } from '../../lib/secureAuth';

/** This dedicated interface never enters ordinary Command/Event state. The native
 * one-shot handoff is available only through current host proof. */
export interface SecureAuthBoundary { available?:boolean; status?():Promise<SecureStatus>; submit(challenge: AuthChallenge, epoch:number, value:string):Promise<void>; configure?(apiId:string,apiHash:string):Promise<void>; takeQr?(challengeId:string,epoch:number):Promise<string> }
const unavailableSecure:SecureAuthBoundary = {available:false,submit:async()=>{throw new Error('Secure handoff unavailable');}};
const LOGIN_START_FAILURE='Telegram could not reconnect. Check account status and try again. Your saved login is kept.';
type Worker = Pick<WorkerClient,'queryAccount'|'beginTelegramLogin'|'cancelTelegramLogin'|'logoutTelegram'|'openThreadsLogin'|'requestTelegramQr'> & Partial<Pick<WorkerClient,'resendTelegramCode'>>;
export function TelegramPanel({view,secure,cancel,begin,logout,requestQr,resendCode,refresh,authoritative,refreshing=false,onOpenRoutes}: {view:AccountView;secure:SecureAuthBoundary;cancel:()=>Promise<void>;begin:()=>Promise<void>;logout:(request:LogoutRequest)=>Promise<void>;requestQr?:(challengeId:string,epoch:number)=>Promise<void>;resendCode?:(challengeId:string,epoch:number)=>Promise<void>;refresh:()=>void;authoritative:boolean;refreshing?:boolean;onOpenRoutes?:()=>void}) {
  const [pending,setPending]=useState(false), [message,setMessage]=useState(''), [dialog,setDialog]=useState(false), [mode,setMode]=useState<LogoutRequest['mode']>('close_local'), [expiredKey,setExpiredKey]=useState<string|null>(null), [inputRevision,setInputRevision]=useState(0), [setup,setSetup]=useState(false);
  const [secureState,setSecureState]=useState<SecureStatus>({available:secure.available===true,configured:false});
  const [loginWaiting,setLoginWaiting]=useState(false);
  const actionSequence=useRef(0),busy=useRef(false),cancelling=useRef(false),mounted=useRef(true);
  const [autoShowQr,setAutoShowQr]=useState(false);const qrHeading=useRef<HTMLHeadingElement>(null);
  useEffect(()=>{let live=true;let timer:ReturnType<typeof setTimeout>|undefined;const check=async()=>{if(!secure.status)return;try{const value=await secure.status();if(live){setSecureState(value);if(!value.available)timer=setTimeout(()=>void check(),1000);}}catch{if(live){setSecureState({available:false,configured:false});timer=setTimeout(()=>void check(),1000);}}};void check();return()=>{live=false;if(timer)clearTimeout(timer);};},[secure,view.status,view.snapshot.epoch,setup]);
  const generation=useRef(0); const key=`${view.snapshot.epoch}:${view.challenge?.challenge_id ?? ''}:${view.challenge?.kind ?? ''}`; const currentKey=useRef(key); currentKey.current=key;
  useEffect(()=>setAutoShowQr(false),[view.snapshot.epoch]);
  useEffect(()=>{if(view.challenge?.kind==='qr'&&authoritative&&secureState.available){qrHeading.current?.focus();qrHeading.current?.scrollIntoView?.({block:'start'});}},[key,authoritative,secureState.available]);
  useEffect(()=>{generation.current++;if(view.challenge)setSetup(false);},[key]);
  const expiry=useRef<{key:string;deadline:number|null}>({key:'',deadline:null});
  useEffect(()=>{if(expiry.current.key!==key)expiry.current={key,deadline:null};const text=view.challenge?.expires_at;if(text){const candidate=Date.parse(text);if(Number.isFinite(candidate))expiry.current.deadline=expiry.current.deadline===null?candidate:Math.min(expiry.current.deadline,candidate);}const deadline=expiry.current.deadline;if(deadline===null)return;const ms=deadline-Date.now();if(ms<=0){setExpiredKey(key);return;}const timer=setTimeout(()=>setExpiredKey(key),ms);return()=>clearTimeout(timer);},[key,view.challenge?.expires_at]);
  useEffect(()=>{if(!authoritative&&!loginWaiting){generation.current++;setSetup(false);}},[authoritative,loginWaiting]);
  useEffect(()=>{mounted.current=true;return()=>{mounted.current=false;generation.current++;actionSequence.current++;};},[]);
  const run=async (operation:()=>Promise<void|false>, success:string, options:{replace?:boolean;login?:boolean;failure?:string|(()=>string)}={}) => {
    if(!mounted.current||(busy.current&&!options.replace))return;
    const sequence=++actionSequence.current;busy.current=true;setPending(true);setMessage('');
    if(options.login)setLoginWaiting(true);
    const current=()=>mounted.current&&actionSequence.current===sequence;
    try{const completed=await operation();if(current()&&completed!==false){if(options.login)setSetup(false);setMessage(success);}}
    catch(error){if(current())setMessage(error instanceof TelegramLoginBlockedError||error instanceof TelegramLoginProblemError?error.message:(typeof options.failure==='function'?options.failure():options.failure)??'Operation unavailable or not confirmed. Checking the current account status.');}
    finally{if(current()){busy.current=false;setPending(false);setLoginWaiting(false);refresh();}}
  };
  const challenge=view.challenge; const snap=view.snapshot;
  const challengeExpired=expiredKey===key || !!(challenge?.expires_at && Date.parse(challenge.expires_at)<=Date.now()) || !!(expiry.current.key===key && expiry.current.deadline!==null && expiry.current.deadline<=Date.now());
  const canBegin=['idle','closed','locally_removed','server_revoked'].includes(view.status)&&snap.auth_state!=='authorizing'&&snap.auth_state!=='ready'&&!snap.revocation_pending;
  const permissionWaiting=loginWaiting&&!challenge&&snap.auth_state!=='authorizing'&&snap.auth_state!=='ready';
  const unavailableHelp=secureState.reason==='app_not_ready'?'The app connection is not ready. Use Reconnect app, then check account status.':secureState.reason==='vault_unavailable'?"ThreadRelay could not access saved login storage. Check account status and try again.":secureState.reason==='telegram_runtime_unavailable'?'Telegram connection is unavailable in this app session. Use Reconnect app, then check account status.':'Telegram sign-in is not ready yet. Check the app connection, then check account status.';
  const cancelLogin=()=>{if(cancelling.current)return;cancelling.current=true;generation.current++;setAutoShowQr(false);setSetup(false);setLoginWaiting(false);void run(cancel,'Setup cancelled in ThreadRelay. An open macOS dialog may still need to be closed. Wait for the connection to close before beginning again.',{replace:true}).finally(()=>{cancelling.current=false;});};
  return <section className="panel"><div className="section-heading"><h2>Telegram personal account</h2><button disabled={refreshing} onClick={refresh}>Check account status</button></div>
    <div className="account-connection"><strong>{!authoritative?'Checking connection':snap.auth_state==='ready'&&snap.network_state==='online'&&!snap.revocation_pending?'Connected to Telegram':snap.auth_state==='authorizing'?'Sign-in in progress':snap.network_state==='connecting'?'Connecting to Telegram':'Telegram not connected'}</strong>{authoritative&&onOpenRoutes&&<button onClick={onOpenRoutes}>Open routes and queue</button>}</div>
    <dl className="details"><div><dt>Account</dt><dd>{snap.auth_state==='ready' && snap.account_id ? `Verified as ${snap.display_name || 'Personal account'}` : snap.account_id ? 'Account saved; sign in to connect' : 'No verified account'}</dd></div><div><dt>Sign-in status</dt><dd>{{ready:'Signed in',authorizing:'Signing in',logged_out:'Not signed in',unknown:'Saved login; verification needed',closed:'Closed'}[snap.auth_state]??snap.auth_state}</dd></div><div><dt>Connection</dt><dd>{{online:'Connected',offline:'Offline',connecting:'Connecting…'}[snap.network_state]??snap.network_state}</dd></div></dl>
    {snap.account_id&&<details className="notice"><summary>Account details</summary><p>Account ID: {snap.account_id}</p></details>}
    {!secureState.available&&<p className="notice">{unavailableHelp}</p>}
    {snap.revocation_pending && <p className="warning">Server revocation pending. To start over, use Account options → Remove local account data offline. This resets this Mac's saved Telegram login; History is kept.</p>}
    {!snap.revocation_pending && view.pending_revocations.length>0 && <details className="notice"><summary>Previous Telegram session</summary><p>You can sign in again. The previous session's server sign-out is unconfirmed; remove that old session from Telegram → Settings → Devices if it is still listed.</p></details>}
    {view.status==='cleanup_pending' && <p className="warning">Local account cleanup is not confirmed. Use Account options → Remove local account data offline to retry deliberately. No new account may replace it yet.</p>}
    {view.status==='authentication_error' && <p className="warning">Telegram could not complete this step. Check your entry and try again, or cancel this login attempt.</p>}
    {view.status==='authentication_unavailable' && !loginWaiting && <p className="warning">Telegram could not reopen the saved session. Cancel this attempt, then try Begin Telegram login. Your saved login is kept.</p>}
    {view.status==='unsupported_auth_state' && <p className="warning">Telegram requested a sign-in method this version cannot display. Cancel the incomplete setup before trying again.</p>}
    {!challenge && ['authentication_unavailable','authentication_error','unsupported_auth_state','account_unavailable','challenge_expired'].includes(view.status) && <button disabled={pending} onClick={()=>{setAutoShowQr(false);void run(cancel,'Close requested. Begin again after the connection reports Closed.');}}>Cancel incomplete setup</button>}
    {!authoritative && <p className="notice">Checking the current connection. Wait for the status refresh before continuing.</p>}
    {permissionWaiting && <section aria-labelledby="telegram-permission-heading"><h3 id="telegram-permission-heading">Opening saved login</h3><p>New saves reopen without a Mac password. An older Keychain login may need one final macOS approval to move it into protected local storage.</p><div className="actions"><button type="button" onClick={cancelLogin}>Cancel setup</button></div></section>}
    {!challenge && !setup && !loginWaiting && canBegin && <><p>{secureState.configured?'Saved credentials are ready. Sign in by phone or QR.':'Connect your Telegram account to send messages.'}</p><div className="actions"><button className="primary" disabled={!authoritative||pending||!secureState.available} onClick={()=>{setAutoShowQr(false);if(secureState.configured)void run(begin,'Login started. The phone or QR step will appear here.',{login:true,failure:LOGIN_START_FAILURE});else setSetup(true);}}>Begin Telegram login</button></div></>}
    {!challenge && snap.auth_state==='authorizing' && view.status!=='challenge_expired' && <div><h3>Waiting for Telegram</h3><p>Waiting for the next login challenge. The phone number or QR step will appear here automatically.</p><button disabled={pending} onClick={cancelLogin}>Cancel login</button></div>}
    {view.status==='closing' && <p role="status">Closing the previous login attempt… Begin login will return when closing is confirmed.</p>}
    {snap.auth_state==='ready'&&snap.network_state==='online'&&<p>Choose a saved chat in Routes, then select a post in History to send.</p>}
    {setup && !loginWaiting && authoritative && !challenge && snap.auth_state!=='authorizing' && <SecureCredentials onCancel={()=>{generation.current++;setSetup(false);}} onSubmit={async(apiId,apiHash)=>{const started=generation.current,startedKey=key;let configured=false;await run(async()=>{if(!secure.configure)throw new Error('Secure storage unavailable');await secure.configure(apiId,apiHash);configured=true;if(!mounted.current||generation.current!==started||currentKey.current!==startedKey)return false;await begin();},'Login started. The phone or QR step will appear here.',{login:true,failure:()=>configured?LOGIN_START_FAILURE:'Secure setup could not be confirmed. Refresh account status before retrying.'});}} />}
    {view.status==='challenge_expired' && <p className="warning">This sign-in step expired. Cancel the incomplete setup, then begin again. Your saved credentials are kept.</p>}
    {challenge?.kind==='password' && !challengeExpired && <div className="notice"><p>Your code or QR step is complete. Telegram now requires your Two-Step Verification password.</p><p>Telegram does not send this password. Enter the password you previously created in Telegram. If you forgot it, use password recovery in the official Telegram app.</p></div>}
    {challenge?.kind==='code' && !challengeExpired && <LoginCodeDelivery delivery={challenge.code_delivery} pending={pending||!authoritative} resend={resendCode?()=>void run(()=>resendCode(challenge.challenge_id,snap.epoch),'Code requested. Wait for Telegram’s next step.'):undefined}/>}
    {challenge && challengeExpired && <div className="warning"><p>Challenge expired. The old input has been cleared. Cancel this attempt, then begin again for a new code.</p><button disabled={pending} onClick={cancelLogin}>Cancel login</button></div>}
    {challenge && !challengeExpired && authoritative && challenge.kind==='qr' && <section aria-label="Telegram QR login"><h3 ref={qrHeading} tabIndex={-1}>Scan the QR code</h3>{secureState.available && secure.takeQr ? <><p>Use Telegram on your phone to link this app. The code is private and expires automatically.</p><SecureQr key={key} challengeId={challenge.challenge_id} epoch={snap.epoch} expiresAt={challenge.expires_at} take={(id,epoch)=>secure.takeQr!(id,epoch)} autoShow={autoShowQr}/></> : <p>Private QR display is unavailable. {unavailableHelp}</p>}<div className="actions"><button disabled={pending} onClick={cancelLogin}>Cancel login</button></div></section>}
    {challenge && !challengeExpired && authoritative && challenge.kind!=='qr' && <><h3>{challenge.kind==='phone'?'Enter your phone number':challenge.kind==='code'?'Enter your Telegram login code':'Enter your two-step password'}</h3><SecureInput key={`${key}:${inputRevision}`} kind={challenge.kind} label={challenge.kind==='phone'?'Phone number':challenge.kind==='code'?'Login code':'Two-step verification password'} submitLabel={challenge.kind==='phone'?'Send login code':challenge.kind==='code'?'Verify code':'Sign in'} hint={challenge.kind==='phone'?'Include your country code, for example +1 202 555 0123.':challenge.kind==='code'?'Enter the current code Telegram sent to your Telegram app or phone.':'Enter the two-step verification password you set in Telegram.'} focusOnMount onCancel={cancelLogin} onSubmit={async value=>{const started=generation.current, startedKey=key; await secure.submit(challenge,snap.epoch,value); if(generation.current===started && currentKey.current===startedKey) refresh();}} /></>}
    {challenge && !challengeExpired && !authoritative && <button disabled={pending} onClick={cancelLogin}>Cancel login</button>}
    {challenge && ['phone','code','password'].includes(challenge.kind) && !challengeExpired && requestQr && <div className="actions"><button disabled={!authoritative||pending} onClick={()=>{generation.current++;setInputRevision(v=>v+1);setAutoShowQr(true);void run(()=>requestQr(challenge.challenge_id,snap.epoch),'QR requested. It will appear here when Telegram responds.');}}>Use QR instead</button></div>}
    {snap.account_id && <div className="actions"><button disabled={!authoritative||pending} onClick={()=>setDialog(true)}>Account options</button></div>}
    {message && challenge?.kind!=='qr' && <p role="status">{message}</p>}
    {dialog && <ConfirmDialog title="Account options" confirmLabel={pending?'Working…':'Confirm account action'} disabled={pending || !authoritative} onCancel={()=>setDialog(false)} onConfirm={()=>void run(async()=>{await logout({mode});setDialog(false);},'Account action requested.')}>
      <p>Closing locally suspends readiness but preserves the login and existing approvals; current identity and destination authority must be reverified before sending. Logout or switching invalidates old account authority and requires routes and approvals to be reviewed again.</p>
      <label className="check"><input type="radio" name="logout-mode" checked={mode==='close_local'} onChange={()=>setMode('close_local')} /> Close locally — release the client, not server logout.</label>
      <label className="check"><input type="radio" name="logout-mode" disabled={!secureState.available||snap.auth_state!=='ready'} checked={mode==='server_logout'} onChange={()=>setMode('server_logout')} /> Log out of Telegram — revoke old route authority now; server revocation stays pending until positively confirmed.</label>
      <label className="check"><input type="radio" name="logout-mode" checked={mode==='remove_local_offline'} onChange={()=>setMode('remove_local_offline')} /> Remove local account data offline — delete this saved session and its vault key after confirmed client close; server revocation remains pending.</label>
      <p className="notice">To replace the account, remove it first, wait for confirmed local cleanup, then provision new credentials. Existing backups are not deleted; terminate pending sessions in Telegram.</p>
    </ConfirmDialog>}
  </section>;
}
export function AccountsView({worker,secure=unavailableSecure,onOpenThreads,onOpenRoutes}: {worker:Worker;secure?:SecureAuthBoundary;onOpenThreads?:()=>void;onOpenRoutes?:()=>void}) {
  const load=useCallback(()=>worker.queryAccount(),[worker]);const account=useResource(load);
  useEffect(()=>{if(account.value||account.loading||!account.error)return;const timer=setTimeout(()=>void account.refresh({background:true}),1000);return()=>clearTimeout(timer);},[account.error,account.loading,account.refresh,account.revision]);
  useEffect(()=>{
    if(!account.value || account.error || account.loading)return;
    const challenge=account.value.challenge;
    const unexpired=!challenge?.expires_at||Date.parse(challenge.expires_at)>Date.now();
    // Observe the backend during every active login challenge. Private submit
    // acknowledgements and independent status reads can arrive in either order.
    // Background reads retain typed DOM input; no authentication is replayed.
    // A deliberate retry consumes an error challenge before the next update;
    // preserve the existing challenge-null wait during that private exchange.
    const waiting=account.value.snapshot.auth_state==='authorizing'&&(!challenge||account.value.status==='authorizing')&&unexpired;
    const lifecycle=['closing','revocation_pending','cleanup_pending'].includes(account.value.status);
    if(!waiting&&!lifecycle)return;
    const timer=setTimeout(()=>void account.refresh({background:true}),5000);
    return()=>clearTimeout(timer);
  },[account.value,account.error,account.loading,account.refresh,account.revision]);
  return <><header className="page-header"><div><h1 tabIndex={-1}>Accounts</h1><p>Connect Telegram for sending. Threads sign-in is optional.</p></div></header>
    <LoadState loading={account.loading} error={account.error} hasValue={account.value!==null} retry={()=>void account.refresh()} />
    {account.value ? <TelegramPanel onOpenRoutes={onOpenRoutes} view={account.value} secure={secure} authoritative={!account.error&&!account.loading} refreshing={account.loading} cancel={()=>worker.cancelTelegramLogin()} begin={()=>worker.beginTelegramLogin()} logout={r=>worker.logoutTelegram(r)} requestQr={(id,epoch)=>worker.requestTelegramQr(id,epoch)} resendCode={worker.resendTelegramCode?(id,epoch)=>worker.resendTelegramCode!(id,epoch):undefined} refresh={()=>void account.refresh()} /> : <section className="panel"><h2>Telegram personal account</h2><p>Waiting for account status. Use Retry if this does not recover.</p></section>}
    <section className="panel"><div className="section-heading"><h2>Threads</h2><button disabled={!onOpenThreads} onClick={onOpenThreads}>Go to Threads</button></div><p>Browse public posts as a guest, or sign in inside ThreadRelay.</p><details><summary>Threads sign-in and privacy</summary><p>Your Threads session is separate from your everyday browser. Sign in inside ThreadRelay; your browser login is not transferred.</p></details></section>
    <details><summary>Telegram setup help</summary><ol>
      <li>Use your existing Telegram account to open <a href="https://my.telegram.org/apps" target="_blank" rel="noopener noreferrer">Telegram API development tools</a> and obtain your own API ID and API hash.</li>
      <li>Choose Begin Telegram login. Enter the API ID and API hash only in ThreadRelay’s private setup fields; they are kept in protected app storage. Do not paste them into a chat.</li>
      <li>Enter your phone number and login code, then your two-step verification password if requested. Or choose Use QR instead and scan it from Telegram on your phone.</li>
    </ol><p>After signing in, open Routes to find and save a chat. Then choose a post in History to prepare a message. Connecting or saving a chat does not send anything.</p><p className="muted">Your saved login stays in protected app storage on this computer. Collection of public Threads posts does not require Telegram.</p></details>
  </>;
}
