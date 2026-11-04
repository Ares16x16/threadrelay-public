import type { WorkerClient } from '../../lib/useWorker';
export type DeliveryClient=Partial<Pick<WorkerClient,'queryAccount'|'queryTopics'|'queryArchiveRevision'|'previewManualHistorySend'|'sendManualHistoryOnce'|'queryManualHistorySend'>>&Pick<WorkerClient,
 'queryDeliveryWorkspace'|'saveMessageTemplate'|'configureDeliveryDraft'|'getDeliveryDraft'|
 'previewDeliveryDraft'|'approveDeliveryDraft'|'queryDeliveryJobs'|'pauseDeliveryJob'|'resumeDeliveryJob'|'cancelDeliveryJob'>;
