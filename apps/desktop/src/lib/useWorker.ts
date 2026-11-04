import type { RouteQueueQuery, ActivityQuery, ActivityPage, SaveMessageTemplateRequest, ConfigureDeliveryDraftRequest, PreviewDeliveryDraftRequest, ApproveDeliveryDraftRequest, DeliveryJobsQuery, DeliveryJobRequest } from './contracts';
import { useMemo } from 'react';
import { invoke as tauriInvoke } from '@tauri-apps/api/core';
import { validateContract, type HistoryItem, type ArchivePostsQuery, type ArchiveRunsQuery, type ArchiveRunDetail, type ObservedPost, isCommand, isEvent, type Command, type Topic, type Route, type ReviewApprovalRequest, type DismissReviewRequest, type DestinationRequest, type SenderRequest, type LogoutRequest, type SessionConfig, type RetentionPolicy, type AutoPolicy, type BulkReviewRequest, type BulkDismissRequest } from './contracts';

type Invoke = (name: string, args: { command: Command }) => Promise<unknown>;
const nativeInvoke: Invoke = (name, args) => tauriInvoke<unknown>(name, args);
let nextId = 0;
export class TelegramLoginBlockedError extends Error {
  constructor(){super('A previous Telegram delivery is unconfirmed. Open Routes → Queue to check it. It will not be sent again automatically.');this.name='TelegramLoginBlockedError';}
}
const loginProblems={
 'Telegram login storage could not be prepared':'The saved Telegram connection needs repair. Close the app and reopen it before trying again.',
 'Secure sign-in permission was not completed':'Saved login could not be opened. Complete or cancel any macOS permission prompt, then try again.',
 'Secure sign-in preparation cancelled':'Sign-in was cancelled. Begin again when you are ready.',
 'telegram_auth:already_started':'A login is already open. Continue the current step below, or cancel it before starting again.',
 'telegram_auth:profile_unavailable':'Telegram could not open the saved connection. Cancel the incomplete setup, then begin again.',
 'telegram_auth:isolation_unavailable':'The archived connection could not be verified. Reopen ThreadRelay, then try again.',
};
export class TelegramLoginProblemError extends Error {
 constructor(reason:keyof typeof loginProblems){super(loginProblems[reason]);this.name='TelegramLoginProblemError';}
}
const routeSaveMessages={
  account_changed:'The Telegram account or connection changed. Refresh account status, then choose and verify the saved chat again.',
  account_unavailable:'Telegram is not signed in for this route. Connect it in Accounts, then verify the saved chat again.',
  destination_unavailable:'This destination needs verification for the current connection. Choose the saved chat and verify it again.',
  destination_not_saved:'This chat is not saved for the current account and sender. Verify and save it in Saved Telegram destinations first.',
  destination_not_sendable:'Telegram has not confirmed free posting permission for this chat. Verify its current permissions before saving.',
  sender_changed:'The selected sender changed. Choose the saved chat and verify its sender again.',
  topic_unavailable:'A selected topic is unavailable or disabled. Choose an enabled topic before saving.',
  route_changed:'This route changed since the editor opened. Reopen the saved route before applying edits.',
  mode_unavailable:'This sending mode is unavailable in the current app build. Choose Review or Dry run.',
};
export class RouteSaveError extends Error {
  constructor(reason:keyof typeof routeSaveMessages){super(routeSaveMessages[reason]);this.name='RouteSaveError';}
}
const manualHistoryMessages={
  connection_required:'Connect Telegram in Accounts, then return here to send.',
  connecting:'Telegram is connecting. Wait until Accounts shows Connected.',
  account_changed:'The account changed. Connect the account selected for this message.',
  busy:'Telegram is checking another operation. Wait, then choose Send now again.',
  outcome_unknown:'An earlier send is unconfirmed. Check Telegram before sending anything else.',
  pending_delivery:'Another delivery is pending. Check its outcome before sending.',
  build_unavailable:'Manual sending is unavailable in this build.',
  job_changed:'This message plan changed or is blocked. Refresh jobs to see the current status.',
};
export class ManualHistoryPrepareError extends Error {
  constructor(reason:keyof typeof manualHistoryMessages){super(manualHistoryMessages[reason]);this.name='ManualHistoryPrepareError';}
}

/** Closed command/event boundary. No listener, process startup or network on construction. */
export class WorkerClient {
  constructor(private readonly invoke: Invoke = nativeInvoke) {}
  private async request(command: Command) {
    if (!isCommand(command)) throw new Error('Invalid local request.');
    let raw: unknown;
    try { raw = await this.invoke('worker_command', { command }); }
    catch(error) {
      const reason=typeof error==='string'?error:error instanceof Error?error.message:'';
      if(command.type==='BeginTelegramLogin'&&Object.hasOwn(loginProblems,reason))throw new TelegramLoginProblemError(reason as keyof typeof loginProblems);
      if((command.type==='SaveRoute'||command.type==='OptInAutoRoute')&&reason==='Delivery is not enabled by connection-only mode')throw new RouteSaveError('mode_unavailable');
      throw new Error('Worker response unavailable.');
    }
    if (!isEvent(raw) || raw.id !== command.id) throw new Error('Worker response unavailable.');
    if (raw.type === 'Error') {
      if(command.type==='BeginTelegramLogin'&&Object.hasOwn(loginProblems,raw.payload.message))throw new TelegramLoginProblemError(raw.payload.message as keyof typeof loginProblems);
      if((command.type==='BeginTelegramLogin'||command.type==='RestoreTelegramSession')&&raw.payload.code==='invalid_request'&&raw.payload.message==='telegram_auth:outcome_unknown')throw new TelegramLoginBlockedError();
      if(command.type==='PreviewManualHistorySend'){
        const reason=raw.payload.message.startsWith('manual_history:')?raw.payload.message.slice(15):'';
        if(raw.payload.code==='invalid_request'&&Object.prototype.hasOwnProperty.call(manualHistoryMessages,reason))throw new ManualHistoryPrepareError(reason as keyof typeof manualHistoryMessages);
        if(raw.payload.code==='unsupported')throw new ManualHistoryPrepareError('build_unavailable');
      }
      if(command.type==='SaveRoute'||command.type==='OptInAutoRoute'){
        const reason=raw.payload.message.startsWith('route_save:')?raw.payload.message.slice(11):'';
        if(raw.payload.code==='invalid_request'&&Object.prototype.hasOwnProperty.call(routeSaveMessages,reason))throw new RouteSaveError(reason as keyof typeof routeSaveMessages);
        if(raw.payload.code==='unsupported')throw new RouteSaveError('mode_unavailable');
      }
      throw new Error(raw.payload.code === 'unsupported' ? 'Unavailable in this build.' : 'The operation could not be completed.');
    }
    return raw;
  }
  private id() { return `desktop-${++nextId}`; }
  async initialize() { const e = await this.request({version:1,id:this.id(),type:'InitializeLocalWorkspace',payload:{}}); if(e.type !== 'State') throw new Error('Worker response unavailable.'); return e.payload; }
  async queryState() { const e = await this.request({version:1,id:this.id(),type:'QueryState',payload:{}}); if(e.type !== 'State') throw new Error('Worker response unavailable.'); return e.payload; }
  async queryAppStatus() { const e=await this.request({version:1,id:this.id(),type:'QueryAppStatus',payload:{}});if(e.type!=='AppStatus')throw new Error('Worker response unavailable.');return e.payload; }
  async queryActivity(payload:ActivityQuery={limit:50}):Promise<ActivityPage> { const e=await this.request({version:1,id:this.id(),type:'QueryActivity',payload});if(e.type!=='Activity')throw new Error('Worker response unavailable.');return e.payload; }
  async queryTelegramTest() { const e=await this.request({version:1,id:this.id(),type:'QueryTelegramTest',payload:{}});if(e.type!=='TelegramTestState')throw new Error('Worker response unavailable.');return e.payload; }
  async sendTelegramTest(payload:import('./contracts').TelegramTestRequest) { return this.ack({version:1,id:this.id(),type:'SendTelegramTest',payload}); }
  async previewManualHistorySend(payload:import('./contracts').DeliveryJobRequest) { const e=await this.request({version:1,id:this.id(),type:'PreviewManualHistorySend',payload});if(e.type!=='ManualHistoryPreview')throw new Error('Worker response unavailable.');return e.payload; }
  async sendManualHistoryOnce(payload:import('./contracts').ManualHistorySendRequest) { return this.ack({version:1,id:this.id(),type:'SendManualHistoryOnce',payload}); }
  async queryManualHistorySend() { const e=await this.request({version:1,id:this.id(),type:'QueryManualHistorySend',payload:{}});if(e.type!=='ManualHistorySendState')throw new Error('Worker response unavailable.');return e.payload; }
  async queryTelegramDestinations(payload:import('./contracts').TelegramDestinationsQuery={cursor:null,limit:30,search:''}) { const e=await this.request({version:1,id:this.id(),type:'QueryTelegramDestinations',payload});if(e.type!=='TelegramDestinations')throw new Error('Worker response unavailable.');return e.payload; }
  async refreshTelegramDestinations(payload:import('./contracts').TelegramDestinationsRefresh={load_more:false}) { return this.ack({version:1,id:this.id(),type:'RefreshTelegramDestinations',payload}); }
  async querySavedTelegramDestinations() { const e=await this.request({version:1,id:this.id(),type:'QuerySavedTelegramDestinations',payload:{}});if(e.type!=='SavedTelegramDestinations')throw new Error('Worker response unavailable.');return e.payload; }
  async saveTelegramDestination(payload:import('./contracts').SaveTelegramDestinationRequest) { return this.ack({version:1,id:this.id(),type:'SaveTelegramDestination',payload}); }
  async queryReadiness(payload: SessionConfig) { const e = await this.request({version:1,id:this.id(),type:'QueryReadiness',payload}); if(e.type !== 'Readiness' || e.payload.mode !== (payload.mode ?? 'review')) throw new Error('Worker response unavailable.'); return e.payload; }
  async start(payload: SessionConfig) { const e = await this.request({version:1,id:this.id(),type:'Start',payload}); if(e.type !== 'State') throw new Error('Worker response unavailable.'); return e.payload; }
  async pause() { const e = await this.request({version:1,id:this.id(),type:'Pause',payload:{}}); if(e.type !== 'State') throw new Error('Worker response unavailable.'); return e.payload; }
  async stopCollection() { const e = await this.request({version:1,id:this.id(),type:'StopCollection',payload:{}}); if(e.type !== 'State') throw new Error('Worker response unavailable.'); return e.payload; }
  async suspendCollection() { const e = await this.request({version:1,id:this.id(),type:'SuspendCollection',payload:{}}); if(e.type !== 'State') throw new Error('Worker response unavailable.'); return e.payload; }
  async stop() {
    const e=await this.request({version:1,id:this.id(),type:'Stop',payload:{}});
    if(e.type!=='State')throw new Error('Worker response unavailable.');
    // Native Stop revokes its entire generation. Reopen only the inert local
    // workspace after confirmed cleanup; collection and authentication stay off.
    if(e.payload.state==='stopped')await this.initialize();
    return e.payload;
  }
  async queryTopics() { const e = await this.request({version:1,id:this.id(),type:'QueryTopics',payload:{}}); if(e.type !== 'Topics') throw new Error('Worker response unavailable.'); return e.payload; }
  async queryRoutes() { const e = await this.request({version:1,id:this.id(),type:'QueryRoutes',payload:{}}); if(e.type !== 'Routes') throw new Error('Worker response unavailable.'); return e.payload; }
  clearRouteQueue(route_id:string) { return this.ack({version:1,id:this.id(),type:'ClearRouteQueue',payload:{route_id}}); }
  async queryRouteQueue(payload:RouteQueueQuery={}) { const e=await this.request({version:1,id:this.id(),type:'QueryRouteQueue',payload});if(e.type!=='RouteQueue')throw new Error('Worker response unavailable.');return e.payload; }
  setRouteQueuePaused(route_id:string,paused:boolean) { return this.ack({version:1,id:this.id(),type:'SetRouteQueuePaused',payload:{route_id,paused}}); }
  cancelRouteQueueItem(delivery_id:string) { return this.ack({version:1,id:this.id(),type:'CancelRouteQueueItem',payload:{delivery_id}}); }
  async queryHistory() { const e = await this.request({version:1,id:this.id(),type:'QueryHistory',payload:{}}); if(e.type !== 'History') throw new Error('Worker response unavailable.'); return e.payload; }
  async queryReview() { const e = await this.request({version:1,id:this.id(),type:'QueryReview',payload:{}}); if(e.type !== 'Review') throw new Error('Worker response unavailable.'); return e.payload; }
  async queryCollection() { const e = await this.request({version:1,id:this.id(),type:'QueryCollection',payload:{}}); if(e.type !== 'Collection') throw new Error('Worker response unavailable.'); return e.payload; }
  async queryArchivePosts(payload:ArchivePostsQuery={}) { const e=await this.request({version:1,id:this.id(),type:'QueryArchivePosts',payload});if(e.type!=='ArchivePosts')throw new Error('Worker response unavailable.');return e.payload; }
  async queryArchiveRuns(payload:ArchiveRunsQuery={}) { const e=await this.request({version:1,id:this.id(),type:'QueryArchiveRuns',payload});if(e.type!=='ArchiveRuns')throw new Error('Worker response unavailable.');return e.payload; }
  private async archiveDetail(kind:'revision'|'run'|'delivery',record_id:string):Promise<unknown> {
    let offset=0,digest:string|undefined,raw='';
    for(let page=0;page<1024;page++) {
      const e=await this.request({version:1,id:this.id(),type:'ReadArchiveDetail',payload:{kind,record_id,offset,...(digest?{expected_digest:digest}:{})}});
      if(e.type!=='ArchiveDetailChunk' || e.payload.kind!==kind || e.payload.record_id!==record_id || e.payload.offset!==offset || (digest && e.payload.digest!==digest))throw new Error('Worker response unavailable.');
      digest=e.payload.digest;raw+=e.payload.data;
      if(raw.length>32_000_000)throw new Error('This saved record is too large to display.');
      if(e.payload.next_offset===null) {
        let value:unknown;try {value=JSON.parse(raw);}catch {throw new Error('Worker response unavailable.');}
        if(!validateContract(kind==='revision'?'ObservedPost':kind==='run'?'ArchiveRunDetail':'HistoryItem',value))throw new Error('Worker response unavailable.');
        return value;
      }
      if(e.payload.next_offset<=offset || e.payload.data.length===0)throw new Error('Worker response unavailable.');
      offset=e.payload.next_offset;
    }
    throw new Error('This saved record is too large to display.');
  }
  async queryArchiveDeliveries(payload:ArchivePostsQuery={}) { const e=await this.request({version:1,id:this.id(),type:'QueryArchiveDeliveries',payload});if(e.type!=='ArchiveDeliveries')throw new Error('Worker response unavailable.');return e.payload; }
  async queryArchiveDelivery(delivery_id:string) { const result=await this.archiveDetail('delivery',delivery_id) as HistoryItem;if(result.delivery.id!==delivery_id)throw new Error('Worker response unavailable.');return result; }
  async queryArchiveRevision(revision_id:string) { return await this.archiveDetail('revision',revision_id) as ObservedPost; }
  async queryArchiveRun(run_id:string) { const result=await this.archiveDetail('run',run_id) as ArchiveRunDetail;if(result.run_id!==run_id)throw new Error('Worker response unavailable.');return result; }
  async queryDeliveryWorkspace() { const e=await this.request({version:1,id:this.id(),type:'QueryDeliveryWorkspace',payload:{}});if(e.type!=='DeliveryWorkspace')throw new Error('Worker response unavailable.');return e.payload; }
  async saveMessageTemplate(payload:SaveMessageTemplateRequest) { const e=await this.request({version:1,id:this.id(),type:'SaveMessageTemplate',payload:payload});if(e.type!=='MessageTemplate')throw new Error('Worker response unavailable.');return e.payload.template; }
  async configureDeliveryDraft(payload:ConfigureDeliveryDraftRequest) { const e=await this.request({version:1,id:this.id(),type:'ConfigureDeliveryDraft',payload:payload});if(e.type!=='DeliveryDraft')throw new Error('Worker response unavailable.');return e.payload.draft; }
  async getDeliveryDraft(draft_id:string) { const e=await this.request({version:1,id:this.id(),type:'GetDeliveryDraft',payload:{draft_id}});if(e.type!=='DeliveryDraft')throw new Error('Worker response unavailable.');return e.payload.draft; }
  async previewDeliveryDraft(payload:PreviewDeliveryDraftRequest) { const e=await this.request({version:1,id:this.id(),type:'PreviewDeliveryDraft',payload:payload});if(e.type!=='DeliveryPreview')throw new Error('Worker response unavailable.');return e.payload; }
  async approveDeliveryDraft(payload:ApproveDeliveryDraftRequest) { const e=await this.request({version:1,id:this.id(),type:'ApproveDeliveryDraft',payload:payload});if(e.type!=='DeliveryJobs')throw new Error('Worker response unavailable.');return e.payload; }
  async queryDeliveryJobs(payload:DeliveryJobsQuery={cursor:null,limit:30,status:null}) { const e=await this.request({version:1,id:this.id(),type:'QueryDeliveryJobs',payload:payload});if(e.type!=='DeliveryJobs')throw new Error('Worker response unavailable.');return e.payload; }
  async pauseDeliveryJob(payload:DeliveryJobRequest) { const e=await this.request({version:1,id:this.id(),type:'PauseDeliveryJob',payload:payload});if(e.type!=='DeliveryJob')throw new Error('Worker response unavailable.');return e.payload.job; }
  async resumeDeliveryJob(payload:DeliveryJobRequest) { const e=await this.request({version:1,id:this.id(),type:'ResumeDeliveryJob',payload:payload});if(e.type!=='DeliveryJob')throw new Error('Worker response unavailable.');return e.payload.job; }
  async cancelDeliveryJob(payload:DeliveryJobRequest) { const e=await this.request({version:1,id:this.id(),type:'CancelDeliveryJob',payload:payload});if(e.type!=='DeliveryJob')throw new Error('Worker response unavailable.');return e.payload.job; }
  async queryAccount() { const e = await this.request({version:1,id:this.id(),type:'QueryAccount',payload:{}}); if(e.type !== 'Account') throw new Error('Worker response unavailable.'); return e.payload; }
  private async ack(c: Command) { const e = await this.request(c); if(e.type !== 'Acknowledged' || e.payload.operation !== c.type) throw new Error('Worker response unavailable.'); }
  async queryRetention() { const e=await this.request({version:1,id:this.id(),type:'QueryRetention',payload:{}});if(e.type!=='Retention')throw new Error('Worker response unavailable.');return e.payload; }
  saveRetention(payload:RetentionPolicy) { return this.ack({version:1,id:this.id(),type:'SaveRetention',payload}); }
  async queryAutoStatus() { const e=await this.request({version:1,id:this.id(),type:'QueryAutoStatus',payload:{}});if(e.type!=='AutoStatus')throw new Error('Worker response unavailable.');return e.payload; }
  saveAutoPolicy(payload:AutoPolicy) { return this.ack({version:1,id:this.id(),type:'SaveAutoPolicy',payload}); }
  async previewAutoRoute(route:Route) { const e=await this.request({version:1,id:this.id(),type:'PreviewAutoRoute',payload:route});if(e.type!=='AutoRoutePreview')throw new Error('Worker response unavailable.');return e.payload; }
  optInAutoRoute(route:Route,expectedReceipt:string) { return this.ack({version:1,id:this.id(),type:'OptInAutoRoute',payload:{route,confirmed:true,expected_receipt:expectedReceipt}}); }
  async testSample(text:string,post_id?:string) { const e=await this.request({version:1,id:this.id(),type:'TestSample',payload:{text,...(post_id?{post_id}:{})}});if(e.type!=='Sample')throw new Error('Worker response unavailable.');return e.payload; }
  async bulkApproveReview(payload:BulkReviewRequest) { const e=await this.request({version:1,id:this.id(),type:'BulkApproveReview',payload});if(e.type!=='BulkReview')throw new Error('Worker response unavailable.');return e.payload; }
  async bulkDismissReview(payload:BulkDismissRequest) { const e=await this.request({version:1,id:this.id(),type:'BulkDismissReview',payload});if(e.type!=='BulkReview')throw new Error('Worker response unavailable.');return e.payload; }
  saveTopic(payload: Topic) { return this.ack({version:1,id:this.id(),type:'SaveTopic',payload}); }
  saveRoute(payload: Route) { return this.ack({version:1,id:this.id(),type:'SaveRoute',payload}); }
  approveReview(payload: ReviewApprovalRequest) { return this.ack({version:1,id:this.id(),type:'ApproveReview',payload}); }
  dismissReview(payload: DismissReviewRequest) { return this.ack({version:1,id:this.id(),type:'DismissReview',payload}); }
  async beginTelegramRecovery(delivery_id:string) { const e=await this.request({version:1,id:this.id(),type:'BeginTelegramRecovery',payload:{delivery_id}});if(e.type!=='TelegramRecoveryState')throw new Error('Recovery response unavailable.');return e.payload; }
  async telegramRecovery() { const e=await this.request({version:1,id:this.id(),type:'QueryTelegramRecovery',payload:{}});if(e.type!=='TelegramRecoveryState')throw new Error('Recovery response unavailable.');return e.payload; }
  restoreTelegramSession() { return this.ack({version:1,id:this.id(),type:'RestoreTelegramSession',payload:{}}); }
  beginTelegramLogin() { return this.ack({version:1,id:this.id(),type:'BeginTelegramLogin',payload:{}}); }
  requestTelegramQr(challenge_id: string, account_epoch: number) { return this.ack({version:1,id:this.id(),type:'RequestTelegramQr',payload:{challenge_id,account_epoch}}); }
  resendTelegramCode(challenge_id: string, account_epoch: number) { return this.ack({version:1,id:this.id(),type:'ResendTelegramCode',payload:{challenge_id,account_epoch}}); }
  cancelTelegramLogin() { return this.ack({version:1,id:this.id(),type:'CancelTelegramLogin',payload:{}}); }
  logoutTelegram(payload: LogoutRequest) { return this.ack({version:1,id:this.id(),type:'LogoutTelegram',payload}); }
  openThreadsLogin() { return this.ack({version:1,id:this.id(),type:'OpenThreadsLogin',payload:{}}); }
  verifyDestination(payload: DestinationRequest) { return this.ack({version:1,id:this.id(),type:'VerifyDestination',payload}); }
  verifySavedMessages() { return this.ack({version:1,id:this.id(),type:'VerifySavedMessages',payload:{}}); }
  selectSender(payload: SenderRequest) { return this.ack({version:1,id:this.id(),type:'SelectSender',payload}); }
  async queryDestination() { const e = await this.request({version:1,id:this.id(),type:'QueryDestination',payload:{}}); if(e.type !== 'DestinationStatus') throw new Error('Worker response unavailable.'); return e.payload; }
}

export function useWorker() { return useMemo(() => new WorkerClient(), []); }
