import { describe, expect, it, vi } from 'vitest';
import { WorkerClient, TelegramLoginBlockedError } from './useWorker';
import type {Route} from './contracts';

const stopped={state:'stopped' as const,counters:{observed:4,matched:2,queued:0,sent:0},attention_reason:null};

it('shows a fixed login preparation reason and hides unknown native details',async()=>{
 const client=new WorkerClient(async()=>{throw 'Telegram login storage could not be prepared';});
 await expect(client.beginTelegramLogin()).rejects.toThrow('The saved Telegram connection needs repair. Close the app and reopen it before trying again.');
 const privateError=new WorkerClient(async()=>{throw 'private arbitrary native detail';});
 await expect(privateError.beginTelegramLogin()).rejects.toThrow('Worker response unavailable.');
});

it('recovers only the chosen uncertain delivery and polls without replaying recovery or starting delivery',async()=>{
 const sent:import('./contracts').Command[]=[];
 const client=new WorkerClient(async(_name,{command})=>{
  sent.push(command);
  return {version:1,id:command.id,type:'TelegramRecoveryState',payload:{state:command.type==='BeginTelegramRecovery'?'closing':'ready_to_login'}};
 });
 expect(await client.beginTelegramRecovery('uncertain-delivery')).toEqual({state:'closing'});
 expect(await client.telegramRecovery()).toEqual({state:'ready_to_login'});
 expect(sent.map(({type,payload})=>({type,payload}))).toEqual([
  {type:'BeginTelegramRecovery',payload:{delivery_id:'uncertain-delivery'}},
  {type:'QueryTelegramRecovery',payload:{}},
 ]);
 const unavailable=vi.fn().mockRejectedValue(new Error('native unavailable'));
 await expect(new WorkerClient(unavailable).beginTelegramRecovery('uncertain-delivery')).rejects.toThrow('Worker response unavailable.');
 expect(unavailable).toHaveBeenCalledTimes(1);
});

it('maps a fixed route-save reason to an actionable error without exposing arbitrary native text',async()=>{
 const route:Route={id:'review',version:1,topic_ids:['t'],destination_id:'-20',mode:'review',enabled:true,account_id:'7',account_epoch:2,sender:{kind:'user',id:'7'}};
 const client=new WorkerClient(async(_name,{command})=>({version:1,id:command.id,type:'Error',payload:{code:'invalid_request',message:'route_save:destination_not_saved'}}));
 await expect(client.saveRoute(route)).rejects.toMatchObject({name:'RouteSaveError',message:'This chat is not saved for the current account and sender. Verify and save it in Saved Telegram destinations first.'});
 const unavailable=new WorkerClient(async()=>{throw 'Delivery is not enabled by connection-only mode';});
 await expect(unavailable.saveRoute(route)).rejects.toMatchObject({name:'RouteSaveError',message:'This sending mode is unavailable in the current app build. Choose Review or Dry run.'});
 const unknown=new WorkerClient(async(_name,{command})=>({version:1,id:command.id,type:'Error',payload:{code:'invalid_request',message:'private fixture detail'}}));
 await expect(unknown.saveRoute(route)).rejects.toThrow('The operation could not be completed.');
});

it('restores only an inert workspace after confirmed Stop and preserves the stop result',async()=>{
 const sent:import('./contracts').Command[]=[];
 const client=new WorkerClient(async(_name,{command})=>{
  sent.push(command);
  return {version:1,id:command.id,type:'State',payload:command.type==='Stop'?stopped:{...stopped,counters:{observed:0,matched:0,queued:0,sent:0}}};
 });
 expect(await client.stop()).toEqual(stopped);
 expect(sent.map(command=>command.type)).toEqual(['Stop','InitializeLocalWorkspace']);
 expect(sent.every(command=>Object.keys(command.payload).length===0)).toBe(true);
});

it.each(['stopping','failed'] as const)('keeps %s cleanup unconfirmed without opening another worker',async state=>{
 const sent:import('./contracts').Command[]=[];
 const client=new WorkerClient(async(_name,{command})=>{sent.push(command);return {version:1,id:command.id,type:'State',payload:{...stopped,state}};});
 expect((await client.stop()).state).toBe(state);
 expect(sent.map(command=>command.type)).toEqual(['Stop']);
});

it('waits for Stop confirmation before inert initialization and never replays a cancelled Start',async()=>{
 const sent:import('./contracts').Command[]=[];let confirmStop!:(value:unknown)=>void,cancelStart!:()=>void;
 const client=new WorkerClient(async(_name,{command})=>{
  sent.push(command);
  if(command.type==='Start')return new Promise((_resolve,reject)=>{cancelStart=()=>reject(new Error('native preparation cancelled'));});
  if(command.type==='Stop')return new Promise(resolve=>{confirmStop=resolve;});
  return {version:1,id:command.id,type:'State',payload:stopped};
 });
 const starting=client.start({feed_url:'https://www.threads.net/',mode:'dry_run'});
 const cancelled=expect(starting).rejects.toThrow('Worker response unavailable.');
 const stopping=client.stop();
 expect(sent.map(command=>command.type)).toEqual(['Start','Stop']);
 confirmStop({version:1,id:sent[1].id,type:'State',payload:stopped});
 await expect(stopping).resolves.toEqual(stopped);
 cancelStart();await cancelled;
 expect(sent.map(command=>command.type)).toEqual(['Start','Stop','InitializeLocalWorkspace']);
});

it('does not retry a failed Stop or a failed inert reconnection',async()=>{
 const rejected=vi.fn().mockRejectedValue(new Error('native cleanup unconfirmed'));
 await expect(new WorkerClient(rejected).stop()).rejects.toThrow('Worker response unavailable.');
 expect(rejected).toHaveBeenCalledTimes(1);
 const sent:import('./contracts').Command[]=[];
 const client=new WorkerClient(async(_name,{command})=>{
  sent.push(command);if(command.type==='InitializeLocalWorkspace')throw new Error('initialization unavailable');
  return {version:1,id:command.id,type:'State',payload:stopped};
 });
 await expect(client.stop()).rejects.toThrow('Worker response unavailable.');
 expect(sent.map(command=>command.type)).toEqual(['Stop','InitializeLocalWorkspace']);
});

describe('typed worker boundary', () => {
  it('correlates and validates responses to a local query', async () => {
    const invoke = vi.fn(async (_name: string, args: {command: {id: string}}) => ({version: 1, id: args.command.id, type: 'Topics', payload: []}));
    const client = new WorkerClient(invoke);
    expect(await client.queryTopics()).toEqual([]);
    expect(invoke).toHaveBeenCalledTimes(1);
  });
  it('rejects mismatched or malformed events without exposing raw error text', async () => {
    const client = new WorkerClient(async () => ({version: 1, id: 'wrong', type: 'Topics', payload: []}));
    await expect(client.queryTopics()).rejects.toThrow('Worker response unavailable.');
  });
});

it('uses only dedicated native actions for secure account data', async()=>{
  const { SecureAuthClient } = await import('./secureAuth');
  const invoke = vi.fn().mockResolvedValue(undefined);
  const secure = new SecureAuthClient(invoke);
  await secure.submit({kind:'code',challenge_id:'challenge',expires_at:null}, 3, 'synthetic-value');
  expect(invoke).toHaveBeenCalledWith('secure_submit', {challengeId:'challenge',kind:'code',epoch:3,value:'synthetic-value'});
  expect(invoke).not.toHaveBeenCalledWith('worker_command', expect.anything());
});

it('carries the backend preview receipt unchanged through the typed opt-in boundary', async()=>{
  const fixtures=await import('../../../../contracts/fixtures.json');
  const {validateContract}=await import('./contracts');
  const preview=fixtures.valid.find(f=>f.contract==='AutoRoutePreview')!.value as unknown as import('./contracts').AutoRoutePreview;
  const sent:import('./contracts').Command[]=[];
  const client=new WorkerClient(async(_name,{command})=>{
    sent.push(command);
    return command.type==='PreviewAutoRoute'?{version:1,id:command.id,type:'AutoRoutePreview',payload:preview}:{version:1,id:command.id,type:'Acknowledged',payload:{operation:command.type}};
  });
  const shown=await client.previewAutoRoute(preview.route);
  expect(shown.destination.audience).toBe(preview.destination.audience);
  await client.optInAutoRoute(shown.route,shown.receipt);
  expect(sent[0]).toMatchObject({type:'PreviewAutoRoute',payload:preview.route});
  expect(sent[1]).toMatchObject({type:'OptInAutoRoute',payload:{route:preview.route,confirmed:true,expected_receipt:'b'.repeat(64)}});
  expect(sent.every(command=>validateContract('Command',command))).toBe(true);
  await expect(client.optInAutoRoute(shown.route,'true')).rejects.toThrow('Invalid local request.');
  expect(sent).toHaveLength(2);
});

it('assembles bounded immutable archive detail and rejects cross-record replies', async()=>{
  const fixtures=await import('../../../../contracts/fixtures.json');
  const post=fixtures.valid.find(f=>f.contract==='ObservedPost')!.value;
  const raw=JSON.stringify(post);const digest='a'.repeat(64);const sent:import('./contracts').Command[]=[];
  const client=new WorkerClient(async(_name,{command})=>{
    sent.push(command);
    if(command.type!=='ReadArchiveDetail')throw new Error('unexpected');
    const offset=command.payload.offset;const end=Math.min(raw.length,offset+80);
    return {version:1,id:command.id,type:'ArchiveDetailChunk',payload:{kind:'revision',record_id:'revision',offset,data:raw.slice(offset,end),next_offset:end<raw.length?end:null,digest}};
  });
  expect(await client.queryArchiveRevision('revision')).toEqual(post);
  expect(sent.length).toBeGreaterThan(1);
  expect(sent[1]).toMatchObject({payload:{expected_digest:digest}});
  const wrong=new WorkerClient(async(_name,{command})=>({version:1,id:command.id,type:'ArchiveDetailChunk',payload:{kind:'revision',record_id:'different',offset:0,data:raw,next_offset:null,digest}}));
  await expect(wrong.queryArchiveRevision('revision')).rejects.toThrow('Worker response unavailable.');
});

it('uses typed local planner commands and unwraps the exact template result',async()=>{
  const template={id:'url',version:1,name:'Post URL',body:'{{url}}',readonly:true,created_at:'2026-11-01T00:00:00Z',updated_at:'2026-11-01T00:00:00Z'};
  const sent:import('./contracts').Command[]=[];
  const client=new WorkerClient(async(_name,{command})=>{sent.push(command);return {version:1,id:command.id,type:'MessageTemplate',payload:{template}};});
  expect(await client.saveMessageTemplate({id:null,expected_version:null,name:'Post URL',body:'{{url}}'})).toEqual(template);
  expect(sent[0].type).toBe('SaveMessageTemplate');
});

it('maps only fixed manual preparation reasons and keeps arbitrary error text private',async()=>{
 const client=new WorkerClient(async(_name,{command})=>({version:1,id:command.id,type:'Error',payload:{code:'invalid_request',message:'manual_history:connection_required'}}));
 await expect(client.previewManualHistorySend({job_id:'job1',version:3})).rejects.toMatchObject({name:'ManualHistoryPrepareError',message:'Connect Telegram in Accounts, then return here to send.'});
 const unknown=new WorkerClient(async(_name,{command})=>({version:1,id:command.id,type:'Error',payload:{code:'invalid_request',message:'manual_history:private-native-detail'}}));
 await expect(unknown.previewManualHistorySend({job_id:'job1',version:3})).rejects.toThrow('The operation could not be completed.');
});


it('restores saved Telegram using a bounded command without starting collection',async()=>{
 const sent:import('./contracts').Command[]=[];
 const client=new WorkerClient(async(_name,{command})=>{sent.push(command);return {version:1,id:command.id,type:'Acknowledged',payload:{operation:command.type}};});
 await client.restoreTelegramSession();
 expect(sent).toEqual([expect.objectContaining({type:'RestoreTelegramSession',payload:{}})]);
});


it('identifies an unconfirmed delivery as the login blocker without exposing native exception text',async()=>{
 const client=new WorkerClient(async(_name,{command})=>({version:1,id:command.id,type:'Error',payload:{code:'invalid_request',message:'telegram_auth:outcome_unknown'}}));
 await expect(client.beginTelegramLogin()).rejects.toBeInstanceOf(TelegramLoginBlockedError);
});
