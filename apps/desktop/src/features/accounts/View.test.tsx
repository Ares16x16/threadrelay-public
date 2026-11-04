import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import type { AccountView, Command } from '../../lib/contracts';
import { WorkerClient, TelegramLoginBlockedError } from '../../lib/useWorker';
import { AccountsView, TelegramPanel } from './View';

it('refreshes account status after a rejected login so the recovery step appears',async()=>{
 const initial:AccountView={snapshot:{account_id:null,epoch:0,auth_state:'logged_out',display_name:null,network_state:'offline',revocation_pending:false},challenge:null,status:'idle',pending_revocations:[]};
 let failed=false;
 const worker={queryAccount:vi.fn(async():Promise<AccountView>=>({...initial,status:failed?'authentication_unavailable':'idle'})),beginTelegramLogin:vi.fn(async()=>{failed=true;throw new Error('unavailable');}),cancelTelegramLogin:vi.fn(),logoutTelegram:vi.fn(),openThreadsLogin:vi.fn(),requestTelegramQr:vi.fn()};
 render(<AccountsView worker={worker} secure={{status:async()=>({available:true,configured:true}),submit:vi.fn()}}/>);
 const begin=await screen.findByRole('button',{name:'Begin Telegram login'});
 await waitFor(()=>expect(begin).toBeEnabled());fireEvent.click(begin);
 expect(await screen.findByText(/Telegram could not reopen the saved session/)).toBeVisible();
 expect(screen.getByRole('button',{name:'Cancel incomplete setup'})).toBeVisible();
 expect(screen.getByText('Telegram could not reconnect. Check account status and try again. Your saved login is kept.')).toBeVisible();
 expect(screen.queryByText('Operation unavailable or not confirmed. Checking the current account status.')).not.toBeInTheDocument();
});
import QRCode from 'qrcode';

it('shows phone entry as the next step without claiming sign-in is blocked',()=>{
 const phone={...view,challenge:{kind:'phone' as const,challenge_id:'phone-step',expires_at:null}};
 render(<TelegramPanel view={phone} secure={{available:true,submit:vi.fn()}} cancel={vi.fn()} begin={vi.fn()} logout={vi.fn()} refresh={vi.fn()} authoritative/>);
 expect(screen.queryByText(/Authentication is blocked if pending sends/)).not.toBeInTheDocument();
 expect(screen.getByRole('heading',{name:'Enter your phone number'})).toBeVisible();
 expect(screen.getByLabelText('Phone number')).toHaveAttribute('type','tel');
 expect(screen.getByLabelText('Phone number')).toHaveFocus();
 expect(screen.getByText(/Include your country code/)).toBeVisible();
 expect(screen.getByRole('button',{name:'Send login code'})).toBeEnabled();
});

it.each(['ready','closed'] as const)('does not offer a new login while status is %s and not restartable',state=>{
 const snapshot={...view.snapshot,auth_state:state,account_id:'123'};
 render(<TelegramPanel view={{...view,snapshot,challenge:null,status:state==='ready'?'ready':'closing'}} secure={{available:true,submit:vi.fn()}} cancel={vi.fn()} begin={vi.fn()} logout={vi.fn()} refresh={vi.fn()} authoritative/>);
 expect(screen.queryByRole('button',{name:'Begin Telegram login'})).not.toBeInTheDocument();
});

it.each([
 ['app_not_ready','Reconnect app'],['vault_unavailable','saved login storage'],['telegram_runtime_unavailable','Telegram connection is unavailable'],
] as const)('explains the current %s sign-in blocker without guessing pending sends',async(reason,copy)=>{
 const status=vi.fn().mockResolvedValue({available:false,configured:false,reason});
 render(<TelegramPanel view={{...view,snapshot:{...view.snapshot,auth_state:'logged_out'},challenge:null,status:'idle'}} secure={{status,submit:vi.fn()}} cancel={vi.fn()} begin={vi.fn()} logout={vi.fn()} refresh={vi.fn()} authoritative/>);
 expect(await screen.findByText(new RegExp(copy))).toBeVisible();
 expect(screen.queryByText(/Authentication is blocked if pending sends/)).not.toBeInTheDocument();
});

it('shows the QR after the phone-step choice without asking for a second display click',async()=>{
 vi.useFakeTimers();
 const context=vi.spyOn(HTMLCanvasElement.prototype,'getContext').mockReturnValue({drawImage:vi.fn()} as unknown as CanvasRenderingContext2D);
 const encode=vi.spyOn(QRCode,'toCanvas').mockImplementation(async canvas=>{canvas.width=24;canvas.height=24;});
 try {
  const takeQr=vi.fn().mockResolvedValue('synthetic-private-flow-qr'),requestQr=vi.fn().mockResolvedValue(undefined);
  const secure={available:true,submit:vi.fn(),takeQr};
  const props={secure,cancel:vi.fn(),begin:vi.fn(),logout:vi.fn(),refresh:vi.fn(),requestQr,authoritative:true};
  const phone={...view,challenge:{kind:'phone' as const,challenge_id:'phone-qr-choice',expires_at:null}};
  const {rerender,container}=render(<TelegramPanel {...props} view={phone}/>);
  await act(async()=>{fireEvent.click(screen.getByRole('button',{name:'Use QR instead'}));});
  expect(requestQr).toHaveBeenCalledWith('phone-qr-choice',3);expect(takeQr).not.toHaveBeenCalled();
  rerender(<TelegramPanel {...props} view={{...view,challenge:{kind:'qr',challenge_id:'deliberate-qr',expires_at:null}}}/>);
  await act(async()=>{await vi.runOnlyPendingTimersAsync();});
  expect(takeQr).toHaveBeenCalledExactlyOnceWith('deliberate-qr',3);
  expect(encode).toHaveBeenCalledTimes(1);expect(screen.queryByRole('button',{name:'Show login QR'})).not.toBeInTheDocument();
  expect(screen.getByText(/Link Desktop Device/)).toBeVisible();
  expect(container.innerHTML).not.toContain('synthetic-private-flow-qr');
 } finally {context.mockRestore();encode.mockRestore();vi.useRealTimers();}
});

const view:AccountView = {snapshot:{account_id:null,epoch:3,auth_state:'authorizing',display_name:null,network_state:'offline',revocation_pending:false},challenge:{kind:'code',challenge_id:'one',expires_at:null},status:'authorizing',pending_revocations:[]};

it('explains that the two-step password is user-created and never delivered as a code',()=>{
 render(<TelegramPanel view={{...view,challenge:{kind:'password',challenge_id:'password',expires_at:null}}} secure={{available:true,submit:vi.fn()}} cancel={vi.fn()} begin={vi.fn()} logout={vi.fn()} refresh={vi.fn()} authoritative/>);
 expect(screen.getByText(/Telegram does not send this password/)).toBeVisible();
 expect(screen.getByRole('heading',{name:'Enter your two-step password'})).toBeVisible();
 expect(screen.queryByRole('button',{name:'Verify code'})).not.toBeInTheDocument();
});
it('clears a code before secure submission and ignores a late superseded response', async () => {
  let finish!:()=>void;
  const submit=vi.fn(()=>new Promise<void>(r=>{finish=r;}));
  const cancel=vi.fn().mockResolvedValue(undefined); const refresh=vi.fn();
  const {rerender}=render(<TelegramPanel view={view} secure={{submit}} cancel={cancel} begin={async()=>{}} logout={async()=>{}} refresh={refresh} authoritative />);
  const input=screen.getByLabelText('Login code');
  fireEvent.change(input,{target:{value:'0012'}});fireEvent.click(screen.getByRole('button',{name:'Verify code'}));
  expect(input).toHaveValue('');
  rerender(<TelegramPanel view={{...view,challenge:{kind:'password',challenge_id:'two',expires_at:null}}} secure={{submit}} cancel={cancel} begin={async()=>{}} logout={async()=>{}} refresh={refresh} authoritative />);
  finish(); await waitFor(()=>expect(submit).toHaveBeenCalledTimes(1));
  expect(screen.getByLabelText('Two-step verification password')).toHaveValue('');
  expect(refresh).not.toHaveBeenCalled();
});

it('observes a delayed challenge after a status read races private submission without replaying input', async()=>{
 vi.useFakeTimers();
 try {
  const phone:AccountView={...view,challenge:{kind:'phone',challenge_id:'phone-race',expires_at:null}};
  let current=phone;let acknowledge!:()=>void;
  const submit=vi.fn(()=>new Promise<void>(resolve=>{acknowledge=resolve;}));
  const queryAccount=vi.fn(async()=>current);
  const worker={queryAccount,beginTelegramLogin:vi.fn(),cancelTelegramLogin:vi.fn(),logoutTelegram:vi.fn(),openThreadsLogin:vi.fn(),requestTelegramQr:vi.fn()};
  await act(async()=>{render(<AccountsView worker={worker} secure={{submit}}/>);});
  fireEvent.change(screen.getByLabelText('Phone number'),{target:{value:'synthetic-private-input'}});
  await act(async()=>{fireEvent.click(screen.getByRole('button',{name:'Send login code'}));});
  // This successful read started before the private ACK and still observes
  // the previous challenge. Foreground refresh supersedes the submit callback.
  await act(async()=>{fireEvent.click(screen.getByRole('button',{name:'Check account status'}));});
  await act(async()=>{acknowledge();});
  current={...view,challenge:{kind:'code',challenge_id:'after-race',expires_at:null}};
  await act(async()=>{await vi.advanceTimersByTimeAsync(5000);});
  expect(screen.getByLabelText('Login code')).toBeVisible();
  expect(submit).toHaveBeenCalledTimes(1);
  expect(worker.beginTelegramLogin).not.toHaveBeenCalled();
  expect(worker.requestTelegramQr).not.toHaveBeenCalled();
 } finally {vi.useRealTimers();}
});

it.each(['phone','code','password'] as const)('observes current %s login status without clearing typed input and stops on auth error', async kind=>{
 vi.useFakeTimers();
 try {
  let current:AccountView={...view,challenge:{kind,challenge_id:'typed',expires_at:null}};
  const queryAccount=vi.fn(async()=>current),submit=vi.fn();
  const worker={queryAccount,beginTelegramLogin:vi.fn(),cancelTelegramLogin:vi.fn(),logoutTelegram:vi.fn(),openThreadsLogin:vi.fn(),requestTelegramQr:vi.fn()};
  await act(async()=>{render(<AccountsView worker={worker} secure={{submit}}/>);});
  const label=kind==='phone'?'Phone number':kind==='code'?'Login code':'Two-step verification password';
  const input=screen.getByLabelText(label);
  fireEvent.change(input,{target:{value:'synthetic-private-input'}});
  current={...current,snapshot:{...current.snapshot,network_state:'online'}};
  await act(async()=>{await vi.advanceTimersByTimeAsync(5000);});
  expect(screen.getByText('Connected')).toBeVisible();
  expect(screen.getByLabelText(label)).toBe(input);expect(input).toHaveValue('synthetic-private-input');
  current={...current,status:'authentication_error'};
  await act(async()=>{await vi.advanceTimersByTimeAsync(5000);});
  const calls=queryAccount.mock.calls.length;
  await act(async()=>{await vi.advanceTimersByTimeAsync(15000);});
  expect(queryAccount).toHaveBeenCalledTimes(calls);expect(submit).not.toHaveBeenCalled();
  expect(worker.beginTelegramLogin).not.toHaveBeenCalled();
 } finally {vi.useRealTimers();}
});

it('continues observing after an explicit retry consumes an authentication-error challenge', async()=>{
 vi.useFakeTimers();
 try {
  let current:AccountView={...view,status:'authentication_error'};
  const submit=vi.fn(async()=>{current={...current,challenge:null};});
  const worker={queryAccount:vi.fn(async()=>current),beginTelegramLogin:vi.fn(),cancelTelegramLogin:vi.fn(),logoutTelegram:vi.fn(),openThreadsLogin:vi.fn(),requestTelegramQr:vi.fn()};
  await act(async()=>{render(<AccountsView worker={worker} secure={{submit}}/>);});
  fireEvent.change(screen.getByLabelText('Login code'),{target:{value:'synthetic-retry'}});
  await act(async()=>{fireEvent.click(screen.getByRole('button',{name:'Verify code'}));});
  expect(screen.getByText(/Waiting for the next login challenge/)).toBeVisible();
  current={...current,snapshot:{...current.snapshot,auth_state:'ready',account_id:'123',network_state:'online'},status:'ready'};
  await act(async()=>{await vi.advanceTimersByTimeAsync(5000);});
  expect(screen.getByText(/Verified as/)).toBeVisible();expect(submit).toHaveBeenCalledTimes(1);
  expect(worker.beginTelegramLogin).not.toHaveBeenCalled();
 } finally {vi.useRealTimers();}
});
it('keeps pending server revocation visible instead of claiming logout', () => {
  render(<TelegramPanel view={{...view,snapshot:{...view.snapshot,account_id:'123',auth_state:'closed',revocation_pending:true},challenge:null,status:'revocation_pending'}} secure={{submit:async()=>{}}} cancel={async()=>{}} begin={async()=>{}} logout={async()=>{}} refresh={()=>{}} authoritative />);
  expect(screen.getByText(/Server revocation pending/)).toBeVisible();
});
it('treats an old removed session as informational and allows fresh sign-in',async()=>{
 const initial:AccountView={snapshot:{account_id:null,epoch:0,auth_state:'logged_out',display_name:null,network_state:'offline',revocation_pending:false},challenge:null,status:'locally_removed',pending_revocations:['123']};
 render(<TelegramPanel view={initial} secure={{available:true,status:async()=>({available:true,configured:false}),submit:vi.fn()}} cancel={vi.fn()} begin={vi.fn()} logout={vi.fn()} refresh={vi.fn()} authoritative/>);
 expect(screen.queryByText(/Server revocation pending/)).not.toBeInTheDocument();
 fireEvent.click(screen.getByText('Previous Telegram session'));
 expect(screen.getByText(/You can sign in again/)).toBeVisible();
 fireEvent.click(screen.getByRole('button',{name:'Begin Telegram login'}));
 expect(await screen.findByLabelText('Developer API ID')).toBeVisible();
});
it('offers explicit QR request only from the current phone challenge', () => {
  const requestQr=vi.fn().mockResolvedValue(undefined);
  render(<TelegramPanel view={{...view,challenge:{kind:'phone',challenge_id:'phone-one',expires_at:null}}} secure={{submit:async()=>{}}} cancel={async()=>{}} begin={async()=>{}} logout={async()=>{}} requestQr={requestQr} refresh={()=>{}} authoritative />);
  fireEvent.click(screen.getByRole('button',{name:'Use QR instead'}));
  expect(requestQr).toHaveBeenCalledWith('phone-one',3);
});
it('clears developer credentials before a failed secure-vault handoff', async () => {
  const configure=vi.fn().mockRejectedValue(new Error('private'));
  render(<TelegramPanel view={{...view,snapshot:{...view.snapshot,auth_state:'logged_out'},challenge:null,status:'idle'}} secure={{available:true,submit:async()=>{},configure}} cancel={async()=>{}} begin={async()=>{}} logout={async()=>{}} refresh={()=>{}} authoritative />);
  fireEvent.click(screen.getByRole('button',{name:'Begin Telegram login'}));
  const id=screen.getByLabelText('Developer API ID'), hash=screen.getByLabelText('Developer API hash');
  fireEvent.change(id,{target:{value:'1234'}});fireEvent.change(hash,{target:{value:'0123456789abcdef0123456789abcdef'}});
  fireEvent.click(screen.getByRole('button',{name:'Store securely and continue'}));
  expect(id).toHaveValue(''); expect(hash).toHaveValue('');
  expect(await screen.findByText(/Secure setup could not be confirmed/)).toBeVisible();
});
it('keeps credential storage and cancellation errors distinct from a current Begin failure',async()=>{
 const cancel=vi.fn().mockRejectedValue(new Error('private cancellation error'));
 render(<TelegramPanel view={{...view,challenge:null}} secure={{available:true,submit:vi.fn()}} cancel={cancel} begin={vi.fn()} logout={vi.fn()} refresh={vi.fn()} authoritative/>);
 fireEvent.click(screen.getByRole('button',{name:'Cancel login'}));
 expect(await screen.findByText('Operation unavailable or not confirmed. Checking the current account status.')).toBeVisible();
 expect(screen.queryByText(/Telegram could not reconnect/)).not.toBeInTheDocument();
});
it('explains an incomplete Begin after new credentials were stored without replaying sign-in',async()=>{
 const configure=vi.fn().mockResolvedValue(undefined),begin=vi.fn().mockRejectedValue(new Error('private native timeout'));
 render(<TelegramPanel view={{...view,snapshot:{...view.snapshot,auth_state:'logged_out'},challenge:null,status:'idle'}} secure={{available:true,submit:vi.fn(),configure}} cancel={vi.fn()} begin={begin} logout={vi.fn()} refresh={vi.fn()} authoritative/>);
 fireEvent.click(screen.getByRole('button',{name:'Begin Telegram login'}));
 fireEvent.change(screen.getByLabelText('Developer API ID'),{target:{value:'123'}});
 fireEvent.change(screen.getByLabelText('Developer API hash'),{target:{value:'0123456789abcdef0123456789abcdef'}});
 fireEvent.click(screen.getByRole('button',{name:'Store securely and continue'}));
 expect(await screen.findByText('Telegram could not reconnect. Check account status and try again. Your saved login is kept.')).toBeVisible();
 expect(configure).toHaveBeenCalledTimes(1);expect(begin).toHaveBeenCalledTimes(1);
 expect(screen.queryByText('private native timeout')).not.toBeInTheDocument();
});
it('renders phone, code, password and QR only from the current challenge', () => {
  const props={secure:{submit:async()=>{}},cancel:async()=>{},begin:async()=>{},logout:async()=>{},refresh:()=>{},authoritative:true};
  const {rerender}=render(<TelegramPanel {...props} view={{...view,challenge:{kind:'phone',challenge_id:'p',expires_at:null}}}/>);
  expect(screen.getByLabelText('Phone number')).toBeVisible();
  rerender(<TelegramPanel {...props} view={view}/>);expect(screen.getByLabelText('Login code')).toBeVisible();
  rerender(<TelegramPanel {...props} view={{...view,challenge:{kind:'password',challenge_id:'pw',expires_at:null}}}/>);expect(screen.getByLabelText('Two-step verification password')).toHaveAttribute('type','password');
  rerender(<TelegramPanel {...props} view={{...view,challenge:{kind:'qr',challenge_id:'q',expires_at:null}}}/>);expect(screen.getByText(/Private QR display is unavailable/)).toBeVisible();
  expect(screen.queryByLabelText('Two-step verification password')).not.toBeInTheDocument();
});
it('removes an expired challenge without retrying or sending', () => {
  const submit=vi.fn(),begin=vi.fn();
  render(<TelegramPanel view={{...view,challenge:{kind:'code',challenge_id:'expired',expires_at:'2020-01-01T00:00:00Z'}}} secure={{submit}} cancel={async()=>{}} begin={begin} logout={async()=>{}} refresh={()=>{}} authoritative/>);
  expect(screen.getByText(/Challenge expired/)).toBeVisible();
  expect(screen.queryByLabelText('Login code')).not.toBeInTheDocument();
  expect(submit).not.toHaveBeenCalled();expect(begin).not.toHaveBeenCalled();
});
it('offers separately confirmed lifecycle actions without claiming remote success', () => {
 const ready={...view,snapshot:{...view.snapshot,account_id:'123',auth_state:'ready' as const},challenge:null,status:'ready' as const};
 render(<TelegramPanel view={ready} secure={{available:true,submit:async()=>{}}} cancel={async()=>{}} begin={async()=>{}} logout={async()=>{}} refresh={()=>{}} authoritative/>);
 fireEvent.click(screen.getByRole('button',{name:'Account options'}));
 expect(screen.getByRole('radio',{name:/Log out of Telegram/})).toBeEnabled();
 expect(screen.getByRole('radio',{name:/Remove local account data offline/})).toBeEnabled();
 expect(screen.getByText(/server revocation stays pending/)).toBeVisible();
 expect(screen.getByRole('radio',{name:/Close locally/})).toBeEnabled();
});
it('observes a later backend challenge through an explicit read without restarting login', async () => {
 let current:AccountView={...view,challenge:null,status:'authorizing'};
 const invoke=vi.fn(async (_name:string,{command}:{command:Command})=>{if(command.type!=='QueryAccount')throw new Error('unexpected mutation');return {version:1,id:command.id,type:'Account',payload:current};});
 const worker=new WorkerClient(invoke);render(<AccountsView worker={worker}/>);
 await screen.findByText('Signing in');
 expect(screen.queryByRole('button',{name:'Begin Telegram login'})).not.toBeInTheDocument();
 expect(screen.getByRole('button',{name:'Cancel login'})).toBeEnabled();
 current={...current,challenge:{kind:'code',challenge_id:'later',expires_at:null}};
 fireEvent.click(screen.getByRole('button',{name:'Check account status'}));
 expect(await screen.findByLabelText('Login code')).toBeVisible();
 expect(invoke).toHaveBeenCalledTimes(2);expect(invoke.mock.calls.every(([,args])=>args.command.type==='QueryAccount')).toBe(true);
});
it('does not revive an already expired challenge when a later projection shifts its timestamp', () => {
 vi.useFakeTimers();vi.setSystemTime(new Date('2030-01-01T00:00:00Z'));
 try {
  const props={secure:{submit:async()=>{}},cancel:async()=>{},begin:async()=>{},logout:async()=>{},refresh:()=>{},authoritative:true};
  const first={...view,challenge:{kind:'code' as const,challenge_id:'same',expires_at:'2030-01-01T00:00:05Z'}};
  const {rerender}=render(<TelegramPanel {...props} view={first}/>);fireEvent.change(screen.getByLabelText('Login code'),{target:{value:'synthetic'}});
  act(()=>vi.advanceTimersByTime(5000));expect(screen.queryByLabelText('Login code')).not.toBeInTheDocument();
  rerender(<TelegramPanel {...props} view={{...first,challenge:{...first.challenge,expires_at:'2030-01-01T00:00:20Z'}}}/>);
  expect(screen.queryByLabelText('Login code')).not.toBeInTheDocument();
 } finally {vi.useRealTimers();}
});
it('clears a challenge input if the account read becomes unconfirmed', async () => {
 let available=true;const worker={queryAccount:vi.fn(async()=>{if(!available)throw new Error('offline');return view;}),beginTelegramLogin:vi.fn(),cancelTelegramLogin:vi.fn(),logoutTelegram:vi.fn(),openThreadsLogin:vi.fn(),requestTelegramQr:vi.fn()};
 const submit=vi.fn();render(<AccountsView worker={worker} secure={{submit}}/>);
 const input=await screen.findByLabelText('Login code') as HTMLInputElement;fireEvent.change(input,{target:{value:'synthetic'}});
 available=false;fireEvent.click(screen.getByRole('button',{name:'Check account status'}));
 expect(await screen.findByRole('alert')).toBeVisible();expect(screen.queryByLabelText('Login code')).not.toBeInTheDocument();expect(input.value).toBe('');expect(screen.getByRole('button',{name:'Cancel login'})).toBeEnabled();expect(submit).not.toHaveBeenCalled();
});
it('observes a later challenge while waiting, without starting or retrying login', async () => {
 vi.useFakeTimers();
 try {
  let current:AccountView={...view,challenge:null,status:'authorizing'};
  const invoke=vi.fn(async (_name:string,{command}:{command:Command})=>{if(command.type!=='QueryAccount')throw new Error('unexpected mutation');return {version:1,id:command.id,type:'Account',payload:current};});
  await act(async()=>{render(<AccountsView worker={new WorkerClient(invoke)}/>);});
  expect(screen.getByText(/Waiting for the next login challenge/)).toBeVisible();
  current={...current,challenge:{kind:'code',challenge_id:'later',expires_at:null}};
  await act(async()=>{await vi.advanceTimersByTimeAsync(5000);});
  expect(screen.getByLabelText('Login code')).toBeVisible();
  current={...current,snapshot:{...current.snapshot,auth_state:'ready',account_id:'123',network_state:'online'},challenge:null,status:'ready'};
  await act(async()=>{await vi.advanceTimersByTimeAsync(5000);});
  expect(screen.getByText(/Verified as/)).toBeVisible();
  const count=invoke.mock.calls.length;await act(async()=>{await vi.advanceTimersByTimeAsync(10000);});expect(invoke).toHaveBeenCalledTimes(count);expect(invoke.mock.calls.every(([,args])=>args.command.type==='QueryAccount')).toBe(true);
 } finally {vi.useRealTimers();}
});
it('stops waiting-state observation after an error until the user checks again', async () => {
 vi.useFakeTimers();
 try {
  let connected=true;const waiting:AccountView={...view,challenge:null,status:'authorizing'};
  const invoke=vi.fn(async (_name:string,{command}:{command:Command})=>{if(!connected)throw new Error('offline');return {version:1,id:command.id,type:'Account',payload:waiting};});
  await act(async()=>{render(<AccountsView worker={new WorkerClient(invoke)}/>);});connected=false;
  await act(async()=>{await vi.advanceTimersByTimeAsync(5000);});const calls=invoke.mock.calls.length;
  await act(async()=>{await vi.advanceTimersByTimeAsync(10000);});expect(invoke).toHaveBeenCalledTimes(calls);
  connected=true;await act(async()=>{fireEvent.click(screen.getByRole('button',{name:'Check account status'}));});
  await act(async()=>{await vi.advanceTimersByTimeAsync(5000);});expect(invoke.mock.calls.length).toBeGreaterThan(calls+1);
 } finally {vi.useRealTimers();}
});
it('continues bounded waiting-state reads even when the backend returns the same object', async () => {
 vi.useFakeTimers();
 try {
  const waiting:AccountView={...view,challenge:null,status:'authorizing'};
  const invoke=vi.fn(async (_name:string,{command}:{command:Command})=>({version:1,id:command.id,type:'Account',payload:waiting}));
  await act(async()=>{render(<AccountsView worker={new WorkerClient(invoke)}/>);});
  for(let i=0;i<3;i++)await act(async()=>{await vi.advanceTimersByTimeAsync(5000);});
  expect(invoke).toHaveBeenCalledTimes(4);
 } finally {vi.useRealTimers();}
});
it('keeps native account setup closed when the secure capability is unavailable',()=>{
 render(<TelegramPanel view={{...view,snapshot:{...view.snapshot,auth_state:'logged_out'},challenge:null,status:'idle'}} secure={{available:false,submit:async()=>{}}} cancel={async()=>{}} begin={async()=>{}} logout={async()=>{}} refresh={()=>{}} authoritative />);
 expect(screen.getByRole('button',{name:'Begin Telegram login'})).toBeDisabled();
});

it('observes a scanned QR without erasing its current surface during the status read', async () => {
 vi.useFakeTimers();
 try {
  const qr:AccountView={...view,challenge:{kind:'qr',challenge_id:'scan-me',expires_at:null}};
  let finish!:(value:AccountView)=>void;
  const queryAccount=vi.fn().mockResolvedValueOnce(qr).mockImplementationOnce(()=>new Promise<AccountView>(resolve=>{finish=resolve;}));
  const worker={queryAccount,beginTelegramLogin:vi.fn(),cancelTelegramLogin:vi.fn(),logoutTelegram:vi.fn(),openThreadsLogin:vi.fn(),requestTelegramQr:vi.fn()};
  const secure={status:vi.fn().mockResolvedValue({available:true,configured:true}),submit:vi.fn(),takeQr:vi.fn()};
  await act(async()=>{render(<AccountsView worker={worker} secure={secure}/>);});
  const canvas=screen.getByRole('img',{name:'Telegram login QR'});
  await act(async()=>{await vi.advanceTimersByTimeAsync(5000);});
  expect(queryAccount).toHaveBeenCalledTimes(2);
  expect(screen.getByRole('img',{name:'Telegram login QR'})).toBe(canvas);
  const ready:AccountView={...view,snapshot:{...view.snapshot,auth_state:'ready',account_id:'123',display_name:'Test account',network_state:'online'},challenge:null,status:'ready'};
  await act(async()=>finish(ready));
  expect(screen.queryByRole('img',{name:'Telegram login QR'})).not.toBeInTheDocument();
  expect(screen.getByText(/Verified as Test account/)).toBeVisible();
  expect(secure.takeQr).not.toHaveBeenCalled();
  expect(worker.beginTelegramLogin).not.toHaveBeenCalled();
 } finally {vi.useRealTimers();}
});

it('clears the private QR and stops status observation after a failed read', async () => {
 vi.useFakeTimers();
 try {
  const qr:AccountView={...view,challenge:{kind:'qr',challenge_id:'scan-me',expires_at:null}};
  const queryAccount=vi.fn().mockResolvedValueOnce(qr).mockRejectedValueOnce(new Error('connection lost'));
  const worker={queryAccount,beginTelegramLogin:vi.fn(),cancelTelegramLogin:vi.fn(),logoutTelegram:vi.fn(),openThreadsLogin:vi.fn(),requestTelegramQr:vi.fn()};
  const secure={status:vi.fn().mockResolvedValue({available:true,configured:true}),submit:vi.fn(),takeQr:vi.fn()};
  await act(async()=>{render(<AccountsView worker={worker} secure={secure}/>);});
  expect(screen.getByRole('img',{name:'Telegram login QR'})).toBeInTheDocument();
  await act(async()=>{await vi.advanceTimersByTimeAsync(5000);});
  expect(screen.queryByRole('img',{name:'Telegram login QR'})).not.toBeInTheDocument();
  expect(screen.getByRole('alert')).toBeVisible();
  await act(async()=>{await vi.advanceTimersByTimeAsync(15000);});
  expect(queryAccount).toHaveBeenCalledTimes(2);
  expect(worker.beginTelegramLogin).not.toHaveBeenCalled();
 } finally {vi.useRealTimers();}
});
it('offers the dedicated QR surface only with the secure capability', () => {
 const secure={available:true,submit:async()=>{},takeQr:vi.fn(async()=> 'synthetic-private-qr')};
 render(<TelegramPanel view={{...view,challenge:{kind:'qr',challenge_id:'qr-1',expires_at:null}}} secure={secure} cancel={async()=>{}} begin={async()=>{}} logout={async()=>{}} refresh={()=>{}} authoritative/>);
 expect(screen.getByRole('button',{name:'Show login QR'})).toBeEnabled();
 expect(screen.queryByText(/Private QR display is unavailable/)).not.toBeInTheDocument();
 expect(secure.takeQr).not.toHaveBeenCalled();
});
it('reuses saved credentials without configuring or displaying API inputs',async()=>{
 const begin=vi.fn().mockResolvedValue(undefined),configure=vi.fn(),status=vi.fn().mockResolvedValue({available:true,configured:true});
 render(<TelegramPanel view={{...view,snapshot:{...view.snapshot,auth_state:'logged_out'},challenge:null,status:'idle'}} secure={{available:false,status,configure,submit:async()=>{}}} cancel={async()=>{}} begin={begin} logout={async()=>{}} refresh={()=>{}} authoritative/>);
 await waitFor(()=>expect(screen.getByRole('button',{name:'Begin Telegram login'})).toBeEnabled());
 fireEvent.click(screen.getByRole('button',{name:'Begin Telegram login'}));
 await waitFor(()=>expect(begin).toHaveBeenCalledTimes(1));expect(configure).not.toHaveBeenCalled();expect(screen.queryByLabelText('Developer API hash')).not.toBeInTheDocument();
});

it.each([true,false])('keeps %s configured login waiting cancellable and ignores its late reply',async configured=>{
 const idle:AccountView={...view,snapshot:{...view.snapshot,auth_state:'logged_out'},challenge:null,status:'idle'};
 let finish!:()=>void;
 const begin=vi.fn(()=>new Promise<void>(resolve=>{finish=resolve;})),cancel=vi.fn().mockResolvedValue(undefined),refresh=vi.fn(),configure=vi.fn().mockResolvedValue(undefined);
 const secure={status:vi.fn().mockResolvedValue({available:true,configured}),configure,submit:vi.fn()};
 render(<TelegramPanel view={idle} secure={secure} cancel={cancel} begin={begin} logout={vi.fn()} refresh={refresh} authoritative/>);
 await waitFor(()=>expect(screen.getByRole('button',{name:'Begin Telegram login'})).toBeEnabled());
 fireEvent.click(screen.getByRole('button',{name:'Begin Telegram login'}));
 if(!configured){
  fireEvent.change(screen.getByLabelText('Developer API ID'),{target:{value:'123'}});
  fireEvent.change(screen.getByLabelText('Developer API hash'),{target:{value:'0123456789abcdef0123456789abcdef'}});
  fireEvent.click(screen.getByRole('button',{name:'Store securely and continue'}));
 }
 await waitFor(()=>expect(begin).toHaveBeenCalledTimes(1));
 expect(screen.getByRole('heading',{name:'Opening saved login'})).toBeVisible();
 expect(screen.queryByText(/Telegram could not reopen the saved session/)).not.toBeInTheDocument();
 expect(screen.getByText('No verified account')).toBeVisible();
 const button=screen.getByRole('button',{name:'Cancel setup'});
 expect(button).toBeEnabled();fireEvent.click(button);fireEvent.click(button);
 await waitFor(()=>expect(cancel).toHaveBeenCalledTimes(1));
 await waitFor(()=>expect(refresh).toHaveBeenCalledTimes(1));
 expect(screen.queryByRole('heading',{name:'Opening saved login'})).not.toBeInTheDocument();
 await act(async()=>finish());
 expect(screen.queryByText(/Login started/)).not.toBeInTheDocument();
 expect(refresh).toHaveBeenCalledTimes(1);expect(begin).toHaveBeenCalledTimes(1);
 expect(configure).toHaveBeenCalledTimes(configured?0:1);
});

it('does not start login after a cancelled private credential handoff resolves',async()=>{
 let finish!:()=>void;
 const configure=vi.fn(()=>new Promise<void>(resolve=>{finish=resolve;})),begin=vi.fn(),cancel=vi.fn().mockResolvedValue(undefined);
 render(<TelegramPanel view={{...view,snapshot:{...view.snapshot,auth_state:'logged_out'},challenge:null,status:'idle'}} secure={{available:true,configure,submit:vi.fn()}} cancel={cancel} begin={begin} logout={vi.fn()} refresh={vi.fn()} authoritative/>);
 fireEvent.click(screen.getByRole('button',{name:'Begin Telegram login'}));
 const id=screen.getByLabelText('Developer API ID'),hash=screen.getByLabelText('Developer API hash');
 fireEvent.change(id,{target:{value:'123'}});fireEvent.change(hash,{target:{value:'0123456789abcdef0123456789abcdef'}});
 fireEvent.click(screen.getByRole('button',{name:'Store securely and continue'}));
 expect(id).toHaveValue('');expect(hash).toHaveValue('');
 fireEvent.click(screen.getByRole('button',{name:'Cancel setup'}));
 await act(async()=>finish());
 expect(cancel).toHaveBeenCalledTimes(1);expect(begin).not.toHaveBeenCalled();
 expect(screen.queryByLabelText('Developer API hash')).not.toBeInTheDocument();
});

it('allows account status reads during native permission waiting without replaying login',async()=>{
 const idle:AccountView={...view,snapshot:{...view.snapshot,auth_state:'logged_out'},challenge:null,status:'idle'};
 let finish!:()=>void;
 const worker={queryAccount:vi.fn().mockResolvedValue(idle),beginTelegramLogin:vi.fn(()=>new Promise<void>(resolve=>{finish=resolve;})),cancelTelegramLogin:vi.fn().mockResolvedValue(undefined),logoutTelegram:vi.fn(),openThreadsLogin:vi.fn(),requestTelegramQr:vi.fn()};
 render(<AccountsView worker={worker} secure={{status:async()=>({available:true,configured:true}),submit:vi.fn()}}/>);
 await waitFor(()=>expect(screen.getByRole('button',{name:'Begin Telegram login'})).toBeEnabled());
 fireEvent.click(screen.getByRole('button',{name:'Begin Telegram login'}));
 expect(await screen.findByRole('heading',{name:'Opening saved login'})).toBeVisible();
 fireEvent.click(screen.getByRole('button',{name:'Check account status'}));
 await waitFor(()=>expect(worker.queryAccount).toHaveBeenCalledTimes(2));
 expect(screen.getByRole('heading',{name:'Opening saved login'})).toBeVisible();
 expect(worker.beginTelegramLogin).toHaveBeenCalledTimes(1);
 fireEvent.click(screen.getByRole('button',{name:'Cancel setup'}));
 await waitFor(()=>expect(worker.queryAccount).toHaveBeenCalledTimes(3));
 await act(async()=>finish());
 expect(worker.queryAccount).toHaveBeenCalledTimes(3);
});

it('moves from permission waiting to the observed phone step without claiming login',async()=>{
 const idle:AccountView={...view,snapshot:{...view.snapshot,auth_state:'logged_out'},challenge:null,status:'idle'};
 let finish!:()=>void;const begin=vi.fn(()=>new Promise<void>(resolve=>{finish=resolve;})),refresh=vi.fn();
 const props={secure:{status:async()=>({available:true,configured:true}),submit:vi.fn()},begin,cancel:vi.fn(),logout:vi.fn(),refresh,authoritative:true};
 const {rerender}=render(<TelegramPanel {...props} view={idle}/>);
 await waitFor(()=>expect(screen.getByRole('button',{name:'Begin Telegram login'})).toBeEnabled());
 fireEvent.click(screen.getByRole('button',{name:'Begin Telegram login'}));
 expect(screen.getByRole('heading',{name:'Opening saved login'})).toBeVisible();
 rerender(<TelegramPanel {...props} view={{...view,challenge:{kind:'phone',challenge_id:'next-phone',expires_at:null}}}/>);
 expect(screen.getByRole('heading',{name:'Enter your phone number'})).toBeVisible();
 expect(screen.queryByRole('heading',{name:'Opening saved login'})).not.toBeInTheDocument();
 expect(screen.queryByText(/Verified as/)).not.toBeInTheDocument();
 await act(async()=>finish());expect(refresh).toHaveBeenCalledTimes(1);
});
it('observes background native initialization without requiring a workspace click or login', async () => {
 vi.useFakeTimers();
 try {
  let ready=false;const idle:AccountView={...view,snapshot:{...view.snapshot,auth_state:'logged_out'},challenge:null,status:'idle'};
  const worker={queryAccount:vi.fn(async()=>{if(!ready)throw new Error('initializing');return idle;}),beginTelegramLogin:vi.fn(),cancelTelegramLogin:vi.fn(),logoutTelegram:vi.fn(),openThreadsLogin:vi.fn(),requestTelegramQr:vi.fn()};
  const secure={status:vi.fn(async()=>({available:ready,configured:false})),configure:vi.fn(),submit:vi.fn()};
  render(<AccountsView worker={worker} secure={secure}/>);
  await act(async()=>{});ready=true;
  await act(async()=>{vi.advanceTimersByTime(1000);});
  expect(screen.getByRole('button',{name:'Begin Telegram login'})).toBeEnabled();
  expect(worker.beginTelegramLogin).not.toHaveBeenCalled();expect(secure.configure).not.toHaveBeenCalled();
 } finally {vi.useRealTimers();}
});
it('refreshes a formerly unavailable auth capability without starting authentication', async () => {
 vi.useFakeTimers();
 try {
  let ready=false;const status=vi.fn(async()=>({available:ready,configured:false})),begin=vi.fn();
  render(<TelegramPanel view={{...view,snapshot:{...view.snapshot,auth_state:'logged_out'},challenge:null,status:'idle'}} secure={{status,configure:vi.fn(),submit:vi.fn()}} cancel={vi.fn()} begin={begin} logout={vi.fn()} refresh={vi.fn()} authoritative/>);
  await act(async()=>{});expect(screen.getByRole('button',{name:'Begin Telegram login'})).toBeDisabled();
  ready=true;await act(async()=>{vi.advanceTimersByTime(1000);});
  expect(screen.getByRole('button',{name:'Begin Telegram login'})).toBeEnabled();expect(begin).not.toHaveBeenCalled();
 } finally {vi.useRealTimers();}
});

it('explains owner-only Telegram setup even while the worker is unavailable',async()=>{
 const worker={queryAccount:vi.fn().mockRejectedValue(new Error('offline')),beginTelegramLogin:vi.fn(),cancelTelegramLogin:vi.fn(),logoutTelegram:vi.fn(),openThreadsLogin:vi.fn(),requestTelegramQr:vi.fn()};
 render(<AccountsView worker={worker}/>);
 const help=screen.getByText('Telegram setup help').closest('details');
 expect(help).not.toHaveAttribute('open');
 fireEvent.click(screen.getByText('Telegram setup help'));
 expect(help).toHaveAttribute('open');
 const link=screen.getByRole('link',{name:'Telegram API development tools'});
 expect(link).toHaveAttribute('href','https://my.telegram.org/apps');expect(link).toHaveAttribute('rel','noopener noreferrer');
 expect(screen.getByText(/API ID and API hash only in ThreadRelay/)).toBeVisible();
 expect(screen.getByText(/phone number and login code/)).toHaveTextContent('two-step verification password');
 expect(screen.getByText(/Use QR instead/)).toBeVisible();
 expect(worker.beginTelegramLogin).not.toHaveBeenCalled();expect(worker.requestTelegramQr).not.toHaveBeenCalled();
});

it('keeps connection actions together and general setup help closed during login',async()=>{
 const idle:AccountView={...view,snapshot:{...view.snapshot,auth_state:'logged_out'},challenge:null,status:'idle'};
 const worker={queryAccount:vi.fn().mockResolvedValue(idle),beginTelegramLogin:vi.fn(),cancelTelegramLogin:vi.fn(),logoutTelegram:vi.fn(),openThreadsLogin:vi.fn(),requestTelegramQr:vi.fn()};
 render(<AccountsView worker={worker} secure={{status:async()=>({available:true,configured:true}),submit:vi.fn()}}/>);
 const begin=await screen.findByRole('button',{name:'Begin Telegram login'});
 await waitFor(()=>expect(begin).toBeEnabled());
 expect(screen.getByRole('button',{name:'Check account status'}).closest('section')).toBe(begin.closest('section'));
 expect(screen.getByText('Telegram setup help').closest('details')).not.toHaveAttribute('open');
 expect(worker.beginTelegramLogin).not.toHaveBeenCalled();
});


it('explains an unconfirmed delivery instead of blaming Mac permission',async()=>{
 const begin=vi.fn().mockRejectedValue(new TelegramLoginBlockedError());
 render(<TelegramPanel view={{...view,snapshot:{...view.snapshot,auth_state:'closed'},challenge:null,status:'closed'}} secure={{status:async()=>({available:true,configured:true}),submit:vi.fn()}} cancel={vi.fn()} begin={begin} logout={vi.fn()} refresh={vi.fn()} authoritative/>);
 const button=await screen.findByRole('button',{name:'Begin Telegram login'});await waitFor(()=>expect(button).toBeEnabled());fireEvent.click(button);
 expect(await screen.findByText(/A previous Telegram delivery is unconfirmed/)).toBeVisible();
 expect(screen.queryByText('Telegram could not reconnect. Check account status and try again. Your saved login is kept.')).not.toBeInTheDocument();
});
it('shows observed Telegram connection and offers the route workspace',async()=>{const onOpenRoutes=vi.fn();const ready:AccountView={snapshot:{account_id:'123',epoch:1,auth_state:'ready',display_name:'Alice',network_state:'online',revocation_pending:false},challenge:null,status:'ready',pending_revocations:[]};const worker={queryAccount:vi.fn().mockResolvedValue(ready),beginTelegramLogin:vi.fn(),cancelTelegramLogin:vi.fn(),logoutTelegram:vi.fn(),openThreadsLogin:vi.fn(),requestTelegramQr:vi.fn()};render(<AccountsView worker={worker} secure={{available:true,submit:vi.fn()}} onOpenRoutes={onOpenRoutes}/>);expect(await screen.findByText('Connected to Telegram')).toBeVisible();fireEvent.click(screen.getByRole('button',{name:'Open routes and queue'}));expect(onOpenRoutes).toHaveBeenCalledOnce();});
