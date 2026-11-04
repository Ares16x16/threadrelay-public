import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const { mockCommand } = vi.hoisted(() => ({ mockCommand: vi.fn() }));
// Tauri invoke is the external process boundary; keep the rendered app real.
vi.mock('@tauri-apps/api/core', () => ({ invoke: mockCommand }));
import App from './App';
beforeEach(()=>{mockCommand.mockReset();});

describe('stopped desktop', () => {
  it('reads initial state without requesting Start or workspace initialization', () => {
    render(<App />);
    expect(screen.getByText('Stopped')).toBeVisible();
    expect(mockCommand).toHaveBeenCalledWith('worker_command',expect.objectContaining({command:expect.objectContaining({type:'QueryState'})}));
    expect(mockCommand.mock.calls.every(([name,args])=>name==='worker_command'&&['QueryState','QueryAppStatus'].includes(args.command.type))).toBe(true);
    expect(screen.getByRole('button', { name: 'Collect in Threads' })).toBeDisabled();
    expect(screen.getByText('Collect posts in Threads. Review and send them from History.')).toBeVisible();
    for (const label of ['Observed', 'Matched', 'Queued', 'Sent']) {
      expect(screen.getByLabelText(`${label} count`)).toHaveTextContent('0');
    }
  });
});

it('prevents shell navigation while a topic save is awaiting acknowledgment', async()=>{
  let finish!:()=>void;
  mockCommand.mockImplementation(async (_name:string,{command}:{command:{id:string;type:string}})=>{
    if(command.type==='QueryTopics')return {version:1,id:command.id,type:'Topics',payload:[]};
    if(command.type==='SaveTopic'){await new Promise<void>(r=>{finish=r;});return {version:1,id:command.id,type:'Acknowledged',payload:{operation:'SaveTopic'}};}
    throw new Error('unexpected');
  });
  const {StrictMode}=await import('react');render(<StrictMode><App/></StrictMode>);
  fireEvent.click(within(screen.getByRole('navigation',{name:'Main navigation'})).getByRole('button',{name:'Topics'}));await screen.findByText('Define what matters');
  fireEvent.click(screen.getByRole('button',{name:'Create topic'}));fireEvent.change(screen.getByLabelText('Topic ID'),{target:{value:'t'}});fireEvent.change(screen.getByLabelText('Description'),{target:{value:'Example'}});fireEvent.click(screen.getByRole('button',{name:'Save topic'}));
  expect(screen.getByRole('button',{name:'Routes'})).toBeDisabled();
  await act(async()=>finish());expect(screen.getByRole('button',{name:'Routes'})).toBeEnabled();
});

it('waits for embedded Threads hide before showing private account inputs and preserves browser on return', async()=>{
  vi.spyOn(HTMLElement.prototype,'getBoundingClientRect').mockReturnValue({x:240,y:220,left:240,top:220,right:840,bottom:620,width:600,height:400,toJSON:()=>({})});
  let finish!:()=>void,closeCount=0;
  mockCommand.mockImplementation(async(name:string)=>{
    if(name==='embedded_threads_open'||name==='embedded_threads_status')return {open:true,generation:7};
    if(name==='embedded_threads_hide'){if(++closeCount===1)await new Promise<void>(resolve=>{finish=resolve;});return;}
    if(name==='embedded_threads_show')return;
    throw new Error('unavailable');
  });
  render(<App/>);
  const nav=within(screen.getByRole('navigation',{name:'Main navigation'}));
  fireEvent.click(nav.getByRole('button',{name:'Threads'}));
  fireEvent.click(screen.getByRole('button',{name:'Open Threads'}));
  await screen.findByText('Threads is open in this window.');
  const persistent=screen.getByLabelText('Embedded Threads viewport');
  fireEvent.click(nav.getByRole('button',{name:'Accounts'}));
  expect(screen.getByRole('heading',{level:1,name:'Threads'})).toBeVisible();
  expect(nav.getByRole('button',{name:'Accounts'})).toBeDisabled();
  expect(screen.getByRole('button',{name:'Close Threads'})).toBeDisabled();
  await act(async()=>finish());
  expect(await screen.findByRole('heading',{level:1,name:'Accounts'})).toBeVisible();
  expect(screen.getByRole('heading',{level:1,name:'Accounts'})).toHaveFocus();
  expect(mockCommand.mock.calls.some(([name])=>name==='embedded_threads_close')).toBe(false);
  fireEvent.click(nav.getByRole('button',{name:'Threads'}));
  await screen.findByText('Threads is open in this window.');
  expect(mockCommand.mock.calls.filter(([name])=>name==='embedded_threads_open')).toHaveLength(1);
  expect(mockCommand.mock.calls.some(([name])=>name==='embedded_threads_show')).toBe(true);
  expect(screen.getByLabelText('Embedded Threads viewport')).toBe(persistent);
  expect(mockCommand.mock.calls.some(([name,args])=>name==='worker_command'&&args.command?.type==='Start')).toBe(false);
});

it('keeps the Threads view when native hiding is unconfirmed', async()=>{
  vi.spyOn(HTMLElement.prototype,'getBoundingClientRect').mockReturnValue({x:240,y:220,left:240,top:220,right:840,bottom:620,width:600,height:400,toJSON:()=>({})});
  mockCommand.mockImplementation(async(name:string)=>{
    if(name==='embedded_threads_open'||name==='embedded_threads_status')return {open:true,generation:7};
    throw new Error('unavailable');
  });
  render(<App/>);
  const nav=within(screen.getByRole('navigation',{name:'Main navigation'}));
  fireEvent.click(nav.getByRole('button',{name:'Threads'}));
  fireEvent.click(screen.getByRole('button',{name:'Open Threads'}));
  await screen.findByText('Threads is open in this window.');
  fireEvent.click(nav.getByRole('button',{name:'Accounts'}));
  expect(await screen.findByText(/Hide Threads could not be confirmed/)).toHaveAttribute('role','alert');
  expect(screen.getByRole('heading',{level:1,name:'Threads'})).toBeVisible();
  expect(screen.queryByRole('heading',{level:1,name:'Accounts'})).not.toBeInTheDocument();
});

it('keeps collection running across internal pages after a native background hide acknowledgment',async()=>{
 vi.spyOn(HTMLElement.prototype,'getBoundingClientRect').mockReturnValue({x:240,y:220,left:240,top:220,right:840,bottom:620,width:600,height:400,toJSON:()=>({})});
 let finishBackground!:()=>void;
 const snapshot={state:'running',counters:{observed:3,matched:1,queued:0,sent:0},attention_reason:null};
 mockCommand.mockImplementation(async(name:string,args:Record<string,unknown>)=>{
  if(name==='worker_command'){
   const command=args.command as {id:string;type:string};
   if(command.type==='QueryState')return {version:1,id:command.id,type:'State',payload:snapshot};
   if(command.type==='QueryAppStatus')return {version:1,id:command.id,type:'AppStatus',payload:{service:'ready',model:{state:'ready',model_id:null},activity:'collection',session:snapshot}};
  }
  if(name==='embedded_threads_open'||name==='embedded_threads_status')return {open:true,generation:7};
  if(name==='embedded_threads_background'){await new Promise<void>(resolve=>{finishBackground=resolve;});return;}
  if(name==='embedded_threads_show')return;
  throw new Error('unavailable');
 });
 render(<App/>);await screen.findByText('Running',{exact:true});
 const nav=within(screen.getByRole('navigation',{name:'Main navigation'}));fireEvent.click(nav.getByRole('button',{name:'Threads'}));
 fireEvent.click(screen.getByRole('button',{name:'Open Threads'}));await screen.findByText('Threads is open in this window.');
 fireEvent.click(nav.getByRole('button',{name:'Accounts'}));await act(async()=>{});
 expect(screen.queryByRole('heading',{level:1,name:'Accounts'})).not.toBeInTheDocument();
 await act(async()=>finishBackground());await screen.findByRole('heading',{level:1,name:'Accounts'});
 expect(screen.getByText('Collection: Running')).toBeVisible();
 expect(mockCommand.mock.calls.some(([name,args])=>name==='embedded_threads_hide'||name==='worker_command'&&['SuspendCollection','Pause','Stop','Start'].includes(args.command?.type))).toBe(false);
 fireEvent.click(nav.getByRole('button',{name:'Threads'}));await screen.findByText('Threads is open in this window.');
 expect(mockCommand.mock.calls.filter(([name])=>name==='embedded_threads_open')).toHaveLength(1);
});

it('resumes only explicitly, restores Threads before Start, and uses the saved configuration',async()=>{
 vi.spyOn(HTMLElement.prototype,'getBoundingClientRect').mockReturnValue({x:240,y:220,left:240,top:220,right:840,bottom:620,width:600,height:400,toJSON:()=>({})});
 const config={feed_url:'https://www.threads.com/',mode:'dry_run',continuous:true,duration_seconds:20,max_posts:2,max_auto_deliveries:0};
 let state='paused';const order:string[]=[];
 mockCommand.mockImplementation(async(name:string,args:Record<string,unknown>)=>{
  const snapshot={state,counters:{observed:3,matched:1,queued:0,sent:0},attention_reason:null,config};
  if(name==='worker_command'){
   const command=args.command as {id:string;type:string};
   if(command.type==='QueryAppStatus')return {version:1,id:command.id,type:'AppStatus',payload:{service:'ready',model:{state:'not_loaded',model_id:null},activity:null,session:snapshot}};
   if(command.type==='QueryState'||command.type==='Start'){if(command.type==='Start'){order.push('Start');state='starting';}return {version:1,id:command.id,type:'State',payload:{...snapshot,state}};}
  }
  if(name==='embedded_threads_open'||name==='embedded_threads_status'){if(name==='embedded_threads_open')order.push('open');return {open:true,generation:7};}
  if(name==='embedded_threads_show'){order.push('show');return;}
  throw new Error('unavailable');
 });
 render(<App/>);await screen.findByText('Collection: Paused');
 const nav=within(screen.getByRole('navigation',{name:'Main navigation'}));fireEvent.click(nav.getByRole('button',{name:'Accounts'}));await screen.findByRole('heading',{level:1,name:'Accounts'});
 expect(order).toEqual([]);fireEvent.click(screen.getByRole('button',{name:'Resume collection'}));
 await screen.findByText('Collection: Starting…');await act(async()=>{});
 expect(order).toEqual(['open','show','Start']);
 const starts=mockCommand.mock.calls.filter(([name,args])=>name==='worker_command'&&args.command?.type==='Start');expect(starts).toHaveLength(1);expect(starts[0][1].command.payload).toEqual(config);
 expect(nav.getByRole('button',{name:'Accounts'})).toBeDisabled();expect(screen.getByRole('button',{name:'Stop collection'})).toBeEnabled();
});

it('global Stop cancels a queued Resume before a late browser-open result can dispatch Start',async()=>{
 vi.spyOn(HTMLElement.prototype,'getBoundingClientRect').mockReturnValue({x:240,y:220,left:240,top:220,right:840,bottom:620,width:600,height:400,toJSON:()=>({})});
 let finish!:(value:{open:boolean;generation:number})=>void;let state='paused';
 mockCommand.mockImplementation(async(name:string,args:Record<string,unknown>)=>{
  if(name==='worker_command'){
   const command=args.command as {id:string;type:string};
   if(command.type==='StopCollection')state='stopped';
   const snapshot={state,counters:{observed:0,matched:0,queued:0,sent:0},attention_reason:null};
   if(command.type==='QueryAppStatus')return {version:1,id:command.id,type:'AppStatus',payload:{service:'ready',model:{state:'not_loaded',model_id:null},activity:null,session:snapshot}};
   if(command.type==='QueryState'||command.type==='StopCollection')return {version:1,id:command.id,type:'State',payload:snapshot};
  }
  if(name==='embedded_threads_open')return new Promise(resolve=>{finish=resolve;});
  if(name==='embedded_threads_status')return {open:true,generation:7};
  if(name==='embedded_threads_show')return;
  throw new Error('unavailable');
 });
 render(<App/>);await screen.findByText('Collection: Paused');fireEvent.click(screen.getByRole('button',{name:'Resume collection'}));
 await act(async()=>{});expect(finish).toBeTypeOf('function');fireEvent.click(screen.getByRole('button',{name:'Stop collection'}));
 await screen.findByText('Collection: Stopped');await act(async()=>finish({open:true,generation:7}));
 expect(mockCommand.mock.calls.some(([name,args])=>name==='worker_command'&&args.command?.type==='Start')).toBe(false);
});

it('opens the read-only Live activity view without starting or reconnecting services',async()=>{
 mockCommand.mockImplementation(async(name:string,args:{command?:{id:string;type:string}})=>{
  const command=args.command;if(name!=='worker_command'||!command)throw new Error('unexpected');
  if(command.type==='QueryActivity')return {version:1,id:command.id,type:'Activity',payload:{entries:[],cursor:'0',has_more:false,older_cursor:null,recording_ok:true,as_of:'2026-11-01T04:00:00Z',session:{state:'stopped',counters:{observed:0,matched:0,queued:0,sent:0},attention_reason:null},model:{state:'not_loaded',model_id:null}}};
  throw new Error('not ready');
 });
 render(<App/>);fireEvent.click(within(screen.getByRole('navigation',{name:'Main navigation'})).getByRole('button',{name:'Live activity'}));
 expect(await screen.findByRole('heading',{level:1,name:'Live activity'})).toBeVisible();expect(await screen.findByText('No activity recorded yet.')).toBeVisible();
 expect(mockCommand.mock.calls.some(([,args])=>['Start','BeginTelegramLogin','InitializeLocalWorkspace'].includes(args.command?.type))).toBe(false);
});
