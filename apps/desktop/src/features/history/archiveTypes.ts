import type { WorkerClient } from '../../lib/useWorker';
export type { ArchiveRevision as RevisionSummary, ArchiveRun as RunSummary, ArchiveRunDetail as RunDetail } from '../../lib/contracts';
export type ArchiveClient=Pick<WorkerClient,'queryArchivePosts'|'queryArchiveRevision'|'queryArchiveRuns'|'queryArchiveRun'>;
