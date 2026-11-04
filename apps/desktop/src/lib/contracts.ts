/* Generated from contracts/protocol.schema.json. Do not edit. */

/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "Visibility".
 */
export type Visibility = "public" | "restricted" | "unknown";
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "Mode".
 */
export type Mode = "dry_run" | "review" | "auto";
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "DeliveryState".
 */
export type DeliveryState = "pending" | "sending" | "sent" | "retryable" | "failed" | "unknown" | "cancelled";
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "SessionState".
 */
export type SessionState =
  "stopped" | "starting" | "running" | "pausing" | "paused" | "stopping" | "needs_attention" | "failed";
/**
 * Canonical signed 64-bit decimal string; never a JavaScript Number. Numeric conversion belongs exclusively in the trusted TDLib boundary. Absolute-end assertion excludes trailing line terminators consistently across runtime validators.
 *
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "TelegramId".
 */
export type TelegramId = string;
/**
 * Application revocation generation, not a TDLib ID.
 *
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "AccountEpoch".
 */
export type AccountEpoch = number;
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "RouteQueueConfig".
 */
export type RouteQueueConfig = {
  [k: string]: unknown;
} & {
  mode: "immediate" | "interval" | "random";
  interval_seconds?: number;
  minimum_seconds?: number;
  maximum_seconds?: number;
  start_at?: string | null;
};
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "Command".
 */
export type Command =
  | {
      version: 1;
      id: string;
      type: "InitializeLocalWorkspace";
      payload: EmptyPayload;
    }
  | {
      version: 1;
      id: string;
      type: "Start";
      payload: SessionConfig;
    }
  | {
      version: 1;
      id: string;
      type: "Pause";
      payload: EmptyPayload;
    }
  | {
      version: 1;
      id: string;
      type: "Stop";
      payload: EmptyPayload;
    }
  | {
      version: 1;
      id: string;
      type: "QueryState";
      payload: EmptyPayload;
    }
  | {
      version: 1;
      id: string;
      type: "SaveTopic";
      payload: Topic;
    }
  | {
      version: 1;
      id: string;
      type: "SaveRoute";
      payload: Route;
    }
  | {
      version: 1;
      id: string;
      type: "Approve";
      payload: Approval;
    }
  | {
      version: 1;
      id: string;
      type: "Dismiss";
      payload: DismissPayload;
    }
  | {
      version: 1;
      id: string;
      type: "QueryTopics";
      payload: EmptyPayload;
    }
  | {
      version: 1;
      id: string;
      type: "QueryRoutes";
      payload: EmptyPayload;
    }
  | {
      version: 1;
      id: string;
      type: "QueryHistory";
      payload: EmptyPayload;
    }
  | {
      version: 1;
      id: string;
      type: "QueryReview";
      payload: EmptyPayload;
    }
  | {
      version: 1;
      id: string;
      type: "QueryAccount";
      payload: EmptyPayload;
    }
  | {
      version: 1;
      id: string;
      type: "BeginTelegramLogin";
      payload: EmptyPayload;
    }
  | {
      version: 1;
      id: string;
      type: "RestoreTelegramSession";
      payload: EmptyPayload;
    }
  | {
      version: 1;
      id: string;
      type: "CheckTelegramLogin";
      payload: EmptyPayload;
    }
  | {
      version: 1;
      id: string;
      type: "CancelTelegramLogin";
      payload: EmptyPayload;
    }
  | {
      version: 1;
      id: string;
      type: "LogoutTelegram";
      payload: LogoutRequest;
    }
  | {
      version: 1;
      id: string;
      type: "VerifyDestination";
      payload: DestinationRequest;
    }
  | {
      version: 1;
      id: string;
      type: "SelectSender";
      payload: SenderRequest;
    }
  | {
      version: 1;
      id: string;
      type: "OpenThreadsLogin";
      payload: EmptyPayload;
    }
  | {
      version: 1;
      id: string;
      type: "ApproveReview";
      payload: ReviewApprovalRequest;
    }
  | {
      version: 1;
      id: string;
      type: "DismissReview";
      payload: DismissReviewRequest;
    }
  | {
      version: 1;
      id: string;
      type: "RequestTelegramQr";
      payload: QrRequest;
    }
  | {
      version: 1;
      id: string;
      type: "QueryDestination";
      payload: EmptyPayload;
    }
  | {
      version: 1;
      id: string;
      type: "QueryReadiness";
      payload: SessionConfig;
    }
  | {
      version: 1;
      id: string;
      type: "QueryRetention";
      payload: EmptyPayload;
    }
  | {
      version: 1;
      id: string;
      type: "SaveRetention";
      payload: RetentionPolicy;
    }
  | {
      version: 1;
      id: string;
      type: "QueryAutoStatus";
      payload: EmptyPayload;
    }
  | {
      version: 1;
      id: string;
      type: "SaveAutoPolicy";
      payload: AutoPolicy;
    }
  | {
      version: 1;
      id: string;
      type: "OptInAutoRoute";
      payload: AutoRouteOptIn;
    }
  | {
      version: 1;
      id: string;
      type: "TestSample";
      payload: SampleRequest;
    }
  | {
      version: 1;
      id: string;
      type: "BulkApproveReview";
      payload: BulkReviewRequest;
    }
  | {
      version: 1;
      id: string;
      type: "BulkDismissReview";
      payload: BulkDismissRequest;
    }
  | {
      version: 1;
      id: string;
      type: "PreviewAutoRoute";
      payload: Route;
    }
  | {
      version: 1;
      id: string;
      type: "VerifySavedMessages";
      payload: EmptyPayload;
    }
  | {
      version: 1;
      id: string;
      type: "QueryCollection";
      payload: EmptyPayload;
    }
  | {
      version: 1;
      id: string;
      type: "QueryArchivePosts";
      payload: ArchivePostsQuery;
    }
  | {
      version: 1;
      id: string;
      type: "QueryArchiveRuns";
      payload: ArchiveRunsQuery;
    }
  | {
      version: 1;
      id: string;
      type: "ReadArchiveDetail";
      payload: ArchiveDetailRequest;
    }
  | {
      version: 1;
      id: string;
      type: "QueryDeliveryWorkspace";
      payload: EmptyPayload;
    }
  | {
      version: 1;
      id: string;
      type: "SaveMessageTemplate";
      payload: SaveMessageTemplateRequest;
    }
  | {
      version: 1;
      id: string;
      type: "ConfigureDeliveryDraft";
      payload: ConfigureDeliveryDraftRequest;
    }
  | {
      version: 1;
      id: string;
      type: "GetDeliveryDraft";
      payload: GetDeliveryDraftRequest;
    }
  | {
      version: 1;
      id: string;
      type: "PreviewDeliveryDraft";
      payload: PreviewDeliveryDraftRequest;
    }
  | {
      version: 1;
      id: string;
      type: "ApproveDeliveryDraft";
      payload: ApproveDeliveryDraftRequest;
    }
  | {
      version: 1;
      id: string;
      type: "QueryDeliveryJobs";
      payload: DeliveryJobsQuery;
    }
  | {
      version: 1;
      id: string;
      type: "PauseDeliveryJob";
      payload: DeliveryJobRequest;
    }
  | {
      version: 1;
      id: string;
      type: "ResumeDeliveryJob";
      payload: DeliveryJobRequest;
    }
  | {
      version: 1;
      id: string;
      type: "CancelDeliveryJob";
      payload: DeliveryJobRequest;
    }
  | {
      version: 1;
      id: string;
      type: "QueryArchiveDeliveries";
      payload: ArchivePostsQuery;
    }
  | {
      version: 1;
      id: string;
      type: "SuspendCollection";
      payload: EmptyPayload;
    }
  | {
      version: 1;
      id: string;
      type: "QueryAppStatus";
      payload: EmptyPayload;
    }
  | {
      version: 1;
      id: string;
      type: "QueryTelegramDestinations";
      payload: TelegramDestinationsQuery;
    }
  | {
      version: 1;
      id: string;
      type: "RefreshTelegramDestinations";
      payload: TelegramDestinationsRefresh;
    }
  | {
      version: 1;
      id: string;
      type: "QueryActivity";
      payload: ActivityQuery;
    }
  | {
      version: 1;
      id: string;
      type: "QueryTelegramTest";
      payload: EmptyPayload;
    }
  | {
      version: 1;
      id: string;
      type: "SendTelegramTest";
      payload: TelegramTestRequest;
    }
  | {
      version: 1;
      id: string;
      type: "QuerySavedTelegramDestinations";
      payload: EmptyPayload;
    }
  | {
      version: 1;
      id: string;
      type: "SaveTelegramDestination";
      payload: SaveTelegramDestinationRequest;
    }
  | {
      version: 1;
      id: string;
      type: "PreviewManualHistorySend";
      payload: DeliveryJobRequest;
    }
  | {
      version: 1;
      id: string;
      type: "SendManualHistoryOnce";
      payload: ManualHistorySendRequest;
    }
  | {
      version: 1;
      id: string;
      type: "QueryManualHistorySend";
      payload: EmptyPayload;
    }
  | {
      version: 1;
      id: string;
      type: "QueryRouteQueue";
      payload: RouteQueueQuery;
    }
  | {
      version: 1;
      id: string;
      type: "SetRouteQueuePaused";
      payload: RouteQueuePauseRequest;
    }
  | {
      version: 1;
      id: string;
      type: "CancelRouteQueueItem";
      payload: RouteQueueCancelRequest;
    }
  | {
      version: 1;
      id: string;
      type: "StopCollection";
      payload: EmptyPayload;
    }
  | {
      version: 1;
      id: string;
      type: "BeginTelegramRecovery";
      payload: {
        delivery_id: string;
      };
    }
  | {
      version: 1;
      id: string;
      type: "QueryTelegramRecovery";
      payload: EmptyPayload;
    }
  | {
      version: 1;
      id: string;
      type: "ResendTelegramCode";
      payload: QrRequest;
    }
  | {
      version: 1;
      id: string;
      type: "ClearRouteQueue";
      payload: {
        route_id: string;
      };
    };
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "AutoPolicy".
 */
export type AutoPolicy =
  | EvaluatedAutoPolicy
  | {
      confirmed: true;
      method: "topic_score";
    };
/**
 * Opaque backend-issued exact Auto preview binding; not a renderer authorization flag.
 *
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "AutoPreviewReceipt".
 */
export type AutoPreviewReceipt = string;
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "DeliveryTiming".
 */
export type DeliveryTiming =
  | {
      mode: "now";
      timezone: string;
    }
  | {
      mode: "at_time";
      timezone: string;
      at: string;
    }
  | {
      mode: "fixed";
      timezone: string;
      start_at: string | null;
      interval_seconds: number;
    }
  | {
      mode: "random";
      timezone: string;
      start_at: string | null;
      minimum_seconds: number;
      maximum_seconds: number;
    }
  | {
      mode: "new_matching";
      timezone: string;
      /**
       * @minItems 1
       * @maxItems 100
       */
      topic_ids: [string, ...string[]];
      interval_seconds: number;
      delivery_mode: "review" | "automatic";
    };
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "DeliverySourcePolicy".
 */
export type DeliverySourcePolicy = "public_only" | "manual_audience_reviewed" | "captured";
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "DeliveryJobStatus".
 */
export type DeliveryJobStatus =
  "draft" | "blocked" | "waiting" | "paused" | "sending" | "sent" | "failed" | "unknown" | "cancelled";
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "Event".
 */
export type Event =
  | {
      version: 1;
      id: string;
      type: "State";
      payload: SessionSnapshot;
    }
  | {
      version: 1;
      id: string;
      type: "Error";
      payload: ErrorPayload;
    }
  | {
      version: 1;
      id: string;
      type: "Topics";
      payload: TopicList;
    }
  | {
      version: 1;
      id: string;
      type: "Routes";
      payload: RouteList;
    }
  | {
      version: 1;
      id: string;
      type: "History";
      payload: HistoryList;
    }
  | {
      version: 1;
      id: string;
      type: "Review";
      payload: ReviewList;
    }
  | {
      version: 1;
      id: string;
      type: "Account";
      payload: AccountView;
    }
  | {
      version: 1;
      id: string;
      type: "Destination";
      payload: DestinationSnapshot;
    }
  | {
      version: 1;
      id: string;
      type: "Acknowledged";
      payload: Acknowledgment;
    }
  | {
      version: 1;
      id: string;
      type: "DestinationStatus";
      payload: DestinationStatus;
    }
  | {
      version: 1;
      id: string;
      type: "Readiness";
      payload: Readiness;
    }
  | {
      version: 1;
      id: string;
      type: "Retention";
      payload: RetentionPolicy;
    }
  | {
      version: 1;
      id: string;
      type: "AutoStatus";
      payload: AutoStatus;
    }
  | {
      version: 1;
      id: string;
      type: "Sample";
      payload: SampleResult;
    }
  | {
      version: 1;
      id: string;
      type: "BulkReview";
      payload: BulkReviewResult;
    }
  | {
      version: 1;
      id: string;
      type: "AutoRoutePreview";
      payload: AutoRoutePreview;
    }
  | {
      version: 1;
      id: string;
      type: "Collection";
      payload: CollectionList;
    }
  | {
      version: 1;
      id: string;
      type: "ArchivePosts";
      payload: ArchivePostsPage;
    }
  | {
      version: 1;
      id: string;
      type: "ArchiveRuns";
      payload: ArchiveRunsPage;
    }
  | {
      version: 1;
      id: string;
      type: "ArchiveDetailChunk";
      payload: ArchiveDetailChunk;
    }
  | {
      version: 1;
      id: string;
      type: "DeliveryWorkspace";
      payload: DeliveryWorkspace;
    }
  | {
      version: 1;
      id: string;
      type: "MessageTemplate";
      payload: MessageTemplateResult;
    }
  | {
      version: 1;
      id: string;
      type: "DeliveryDraft";
      payload: DeliveryDraftResult;
    }
  | {
      version: 1;
      id: string;
      type: "DeliveryPreview";
      payload: DeliveryPreview;
    }
  | {
      version: 1;
      id: string;
      type: "DeliveryJobs";
      payload: DeliveryJobs;
    }
  | {
      version: 1;
      id: string;
      type: "DeliveryJob";
      payload: DeliveryJobResult;
    }
  | {
      version: 1;
      id: string;
      type: "ArchiveDeliveries";
      payload: ArchiveDeliveriesPage;
    }
  | {
      version: 1;
      id: string;
      type: "AppStatus";
      payload: AppStatus;
    }
  | {
      version: 1;
      id: string;
      type: "TelegramDestinations";
      payload: TelegramDestinations;
    }
  | {
      version: 1;
      id: string;
      type: "Activity";
      payload: ActivityPage;
    }
  | {
      version: 1;
      id: string;
      type: "TelegramTestState";
      payload: TelegramTestState;
    }
  | {
      version: 1;
      id: string;
      type: "SavedTelegramDestinations";
      payload: SavedTelegramDestinations;
    }
  | {
      version: 1;
      id: string;
      type: "ManualHistoryPreview";
      payload: ManualHistoryPreview;
    }
  | {
      version: 1;
      id: string;
      type: "ManualHistorySendState";
      payload: ManualHistorySendState;
    }
  | {
      version: 1;
      id: string;
      type: "RouteQueue";
      payload: RouteQueue;
    }
  | {
      version: 1;
      id: string;
      type: "TelegramRecoveryState";
      payload: TelegramRecoveryState;
    };
/**
 * @maxItems 10000
 *
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "TopicList".
 */
export type TopicList = Topic[];
/**
 * @maxItems 10000
 *
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "RouteList".
 */
export type RouteList = Route[];
/**
 * @maxItems 10000
 *
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "HistoryList".
 */
export type HistoryList = HistoryItem[];
/**
 * @maxItems 10000
 *
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "ReviewList".
 */
export type ReviewList = ReviewItem[];
/**
 * @maxItems 100
 *
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "CollectionList".
 */
export type CollectionList = CollectionItem[];

/**
 * Private parent/child IPC. Four-byte unsigned big-endian byte length; UTF-8 JSON; payload 1..1048576 bytes. No generic setup, URL, file, process or shell bridge.
 */
export interface ThreadRelayProtocolV1 {
  Visibility: Visibility;
  Mode: Mode;
  DeliveryState: DeliveryState;
  SessionState: SessionState;
  PostIdentity: PostIdentity;
  ExtractionIssue: ExtractionIssue;
  QuoteAttribution: QuoteAttribution;
  ObservedPost: ObservedPost;
  Topic: Topic;
  TopicResult: TopicResult;
  Classification: Classification;
  Route: Route;
  Approval: Approval;
  RouteReference: RouteReference;
  Delivery: Delivery;
  SessionConfig: SessionConfig;
  SessionCounters: SessionCounters;
  SessionSnapshot: SessionSnapshot;
  DeliveryProposal: DeliveryProposal;
  AuthorizationResult: AuthorizationResult;
  ModelManifest: ModelManifest;
  VerificationResult: VerificationResult;
  ShutdownReport: ShutdownReport;
  PruneReport: PruneReport;
  RecoverySummary: RecoverySummary;
  DeliveryEvent: DeliveryEvent;
  DatasetRecord: DatasetRecord;
  Dataset: Dataset;
  EvaluationReport: EvaluationReport;
  EmptyPayload: EmptyPayload;
  DismissPayload: DismissPayload;
  ErrorPayload: ErrorPayload;
  Command: Command;
  Event: Event;
  TelegramId: TelegramId;
  AccountEpoch: AccountEpoch;
  SenderIdentity: SenderIdentity;
  AccountSnapshot: AccountSnapshot;
  AuthChallenge: AuthChallenge;
  DestinationPermissions: DestinationPermissions;
  DestinationSnapshot: DestinationSnapshot;
  TelegramAttempt: TelegramAttempt;
  TopicList: TopicList;
  RouteList: RouteList;
  HistoryItem: HistoryItem;
  HistoryList: HistoryList;
  ReviewList: ReviewList;
  Acknowledgment: Acknowledgment;
  AccountView: AccountView;
  DestinationRequest: DestinationRequest;
  SenderRequest: SenderRequest;
  LogoutRequest: LogoutRequest;
  ReviewDisclosure: ReviewDisclosure;
  ReviewApprovalRequest: ReviewApprovalRequest;
  DismissReviewRequest: DismissReviewRequest;
  ReviewItem: ReviewItem;
  QrRequest: QrRequest;
  DestinationStatus: DestinationStatus;
  TopicSummary: TopicSummary;
  Readiness: Readiness;
  RetentionPolicy: RetentionPolicy;
  AutoPolicy: AutoPolicy;
  AutoStatus: AutoStatus;
  AutoRouteOptIn: AutoRouteOptIn;
  SampleRequest: SampleRequest;
  SampleResult: SampleResult;
  BulkReviewRequest: BulkReviewRequest;
  BulkDismissRequest: BulkDismissRequest;
  BulkReviewResult: BulkReviewResult;
  AutoPreviewReceipt: AutoPreviewReceipt;
  AutoRoutePreview: AutoRoutePreview;
  CollectionItem: CollectionItem;
  CollectionList: CollectionList;
  ObservedMetadata: ObservedMetadata;
  ArchiveRevision: ArchiveRevision;
  ArchiveRun: ArchiveRun;
  ArchiveRunDetail: ArchiveRunDetail;
  ArchivePostsQuery: ArchivePostsQuery;
  ArchiveRunsQuery: ArchiveRunsQuery;
  ArchivePostsPage: ArchivePostsPage;
  ArchiveRunsPage: ArchiveRunsPage;
  ArchiveDetailRequest: ArchiveDetailRequest;
  ArchiveDetailChunk: ArchiveDetailChunk;
  WorkflowTarget: WorkflowTarget;
  DeliveryTiming: DeliveryTiming;
  MessageTemplate: MessageTemplate;
  DeliveryDraft: DeliveryDraft;
  DeliveryPreviewItem: DeliveryPreviewItem;
  DeliveryPreview: DeliveryPreview;
  DeliveryJobStatus: DeliveryJobStatus;
  DeliveryJob: DeliveryJob;
  DeliveryWorkspace: DeliveryWorkspace;
  DeliveryJobs: DeliveryJobs;
  SaveMessageTemplateRequest: SaveMessageTemplateRequest;
  ConfigureDeliveryDraftRequest: ConfigureDeliveryDraftRequest;
  GetDeliveryDraftRequest: GetDeliveryDraftRequest;
  PreviewDeliveryDraftRequest: PreviewDeliveryDraftRequest;
  ApproveDeliveryDraftRequest: ApproveDeliveryDraftRequest;
  DeliveryJobsQuery: DeliveryJobsQuery;
  DeliveryJobRequest: DeliveryJobRequest;
  MessageTemplateResult: MessageTemplateResult;
  DeliveryDraftResult: DeliveryDraftResult;
  DeliveryJobResult: DeliveryJobResult;
  ArchiveDelivery: ArchiveDelivery;
  ArchiveDeliveriesPage: ArchiveDeliveriesPage;
  DeliverySourcePolicy: DeliverySourcePolicy;
  ModelRuntimeStatus: ModelRuntimeStatus;
  AppStatus: AppStatus;
  TelegramDestinationsQuery: TelegramDestinationsQuery;
  TelegramDestinationsRefresh: TelegramDestinationsRefresh;
  TelegramDestinationCandidate: TelegramDestinationCandidate;
  TelegramDestinations: TelegramDestinations;
  ActivityQuery: ActivityQuery;
  ActivityEntry: ActivityEntry;
  ActivityPage: ActivityPage;
  RecoverySnapshot: RecoverySnapshot;
  TelegramTestRequest: TelegramTestRequest;
  TelegramTestState: TelegramTestState;
  SavedTelegramDestination: SavedTelegramDestination;
  SavedTelegramDestinations: SavedTelegramDestinations;
  SaveTelegramDestinationRequest: SaveTelegramDestinationRequest;
  ManualHistorySendRequest: ManualHistorySendRequest;
  ManualHistoryPreview: ManualHistoryPreview;
  ManualHistorySendState: ManualHistorySendState;
  EvaluatedAutoPolicy: EvaluatedAutoPolicy;
  RouteQueueConfig: RouteQueueConfig;
  RouteQueueQuery: RouteQueueQuery;
  RouteQueuePauseRequest: RouteQueuePauseRequest;
  RouteQueueCancelRequest: RouteQueueCancelRequest;
  RouteQueueItem: RouteQueueItem;
  RouteQueue: RouteQueue;
  TelegramRecoveryState: TelegramRecoveryState;
  AuthCodeDelivery: AuthCodeDelivery;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "PostIdentity".
 */
export interface PostIdentity {
  post_id: string;
  permalink: string;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "ExtractionIssue".
 */
export interface ExtractionIssue {
  code: string;
  detail: string;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "QuoteAttribution".
 */
export interface QuoteAttribution {
  author: string;
  text: string;
  permalink: string | null;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "ObservedPost".
 */
export interface ObservedPost {
  post_id: string;
  permalink: string;
  author: string;
  text: string;
  content_hash: string;
  visibility: Visibility;
  /**
   * @maxItems 100
   */
  quality_flags: ExtractionIssue[];
  observed_at: string;
  source_session_id: string;
  quote: QuoteAttribution | null;
  observed_metadata?: ObservedMetadata;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "ObservedMetadata".
 */
export interface ObservedMetadata {
  display_name?: string;
  published_label?: string;
  published_at?: string;
  /**
   * @maxItems 20
   */
  engagement?:
    | []
    | [
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        }
      ]
    | [
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        }
      ]
    | [
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        }
      ]
    | [
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        }
      ]
    | [
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        }
      ]
    | [
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        }
      ]
    | [
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        }
      ]
    | [
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        }
      ]
    | [
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        }
      ]
    | [
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        }
      ]
    | [
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        }
      ]
    | [
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        }
      ]
    | [
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        }
      ]
    | [
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        }
      ]
    | [
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        }
      ]
    | [
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        }
      ]
    | [
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        }
      ]
    | [
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        }
      ]
    | [
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        }
      ]
    | [
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        },
        {
          kind: "likes" | "replies" | "reposts" | "quotes" | "views" | "shares";
          label: string;
          count?: number;
        }
      ];
  /**
   * @maxItems 20
   */
  media?:
    | []
    | [
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        }
      ]
    | [
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        }
      ]
    | [
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        }
      ]
    | [
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        }
      ]
    | [
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        }
      ]
    | [
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        }
      ]
    | [
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        }
      ]
    | [
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        }
      ]
    | [
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        }
      ]
    | [
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        }
      ]
    | [
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        }
      ]
    | [
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        }
      ]
    | [
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        }
      ]
    | [
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        }
      ]
    | [
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        }
      ]
    | [
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        }
      ]
    | [
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        }
      ]
    | [
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        }
      ]
    | [
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        }
      ]
    | [
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        },
        {
          kind: "image" | "video" | "link";
          url?: string;
          alt?: string;
        }
      ];
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "Topic".
 */
export interface Topic {
  id: string;
  version: number;
  description: string;
  /**
   * @maxItems 100
   */
  positive_examples: string[];
  /**
   * @maxItems 100
   */
  negative_examples: string[];
  /**
   * @maxItems 100
   */
  exclusions: string[];
  enabled: boolean;
  /**
   * Uncalibrated model relevance cutoff; not a probability. Missing uses 0.9.
   */
  minimum_score?: number;
}
/**
 * Optional score is not a calibrated probability; outputs contain no destinations or actions.
 *
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "TopicResult".
 */
export interface TopicResult {
  topic_id: string;
  match: boolean;
  abstained: boolean;
  score?: number | null;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "Classification".
 */
export interface Classification {
  post_id: string;
  model_version: string;
  /**
   * @maxItems 100
   */
  topic_results: TopicResult[];
  abstained: boolean;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "Route".
 */
export interface Route {
  id: string;
  version: number;
  /**
   * @minItems 1
   * @maxItems 100
   */
  topic_ids: [string, ...string[]];
  destination_id: TelegramId;
  mode: Mode;
  enabled: boolean;
  account_id: TelegramId;
  account_epoch: AccountEpoch;
  sender: SenderIdentity;
  /**
   * Explicit exact-route consent for link-only sharing of audience-unverified visible sources under the topic_score policy. Omitted or false preserves public-only Auto.
   */
  allow_unverified_source?: boolean;
  message_template?: string;
  queue?: RouteQueueConfig;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "SenderIdentity".
 */
export interface SenderIdentity {
  kind: "user" | "chat";
  id: TelegramId;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "Approval".
 */
export interface Approval {
  post_id: string;
  content_hash: string;
  destination_id: TelegramId;
  route_version: number;
  id: string;
  route_id: string;
  account_id: TelegramId;
  account_epoch: AccountEpoch;
  sender: SenderIdentity;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "RouteReference".
 */
export interface RouteReference {
  route_id: string;
  route_version: number;
  approval_id: string | null;
}
/**
 * id is the durable delivery/operation identity, deduplicated by account_id+post_id+destination_id independently of sender. message_id is a legacy final_message_id alias, never the temporary ID. claim/attempt correlation is not authorization to send.
 *
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "Delivery".
 */
export interface Delivery {
  id: string;
  post_id: string;
  destination_id: TelegramId;
  content_hash: string;
  state: DeliveryState;
  attempt_id: string | null;
  message_id: TelegramId | null;
  /**
   * @maxItems 100
   */
  route_refs: RouteReference[];
  account_id: TelegramId;
  account_epoch: AccountEpoch;
  sender: SenderIdentity;
  temporary_message_id: TelegramId | null;
  final_message_id: TelegramId | null;
  request_extra: string | null;
  workflow_job_id?: string;
}
/**
 * Collection and automatic delivery limits are separate. Unlimited automatic delivery requires explicit unlimited_auto_deliveries=true; zero max_auto_deliveries still disables automatic starts when the unlimited flag is absent or false.
 *
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "SessionConfig".
 */
export interface SessionConfig {
  feed_url: string;
  mode?: "dry_run" | "review" | "auto";
  duration_seconds?: number;
  max_posts?: number;
  max_auto_deliveries?: number;
  /**
   * Keep collecting across internal pages until explicit Pause/Stop or a hard dependency failure. Ignores duration/post caps; automatic delivery budgets still apply.
   */
  continuous?: boolean;
  /**
   * @minItems 1
   * @maxItems 1
   */
  route_ids?: [string];
  /**
   * Explicitly keep automatic delivery running without a per-session count cap. Pause/Stop, route timing and Telegram-required waits still apply. Omitted or false keeps max_auto_deliveries, including zero disabling sends.
   */
  unlimited_auto_deliveries?: boolean;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "SessionCounters".
 */
export interface SessionCounters {
  observed: number;
  matched: number;
  queued: number;
  sent: number;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "SessionSnapshot".
 */
export interface SessionSnapshot {
  state: "stopped" | "starting" | "running" | "pausing" | "paused" | "stopping" | "needs_attention" | "failed";
  counters: SessionCounters;
  attention_reason: string | null;
  config?: SessionConfig1;
}
/**
 * Last successfully promoted session configuration; absent during a new unpromoted start.
 */
export interface SessionConfig1 {
  feed_url: string;
  mode?: "dry_run" | "review" | "auto";
  duration_seconds?: number;
  max_posts?: number;
  max_auto_deliveries?: number;
  /**
   * Keep collecting across internal pages until explicit Pause/Stop or a hard dependency failure. Ignores duration/post caps; automatic delivery budgets still apply.
   */
  continuous?: boolean;
  /**
   * @minItems 1
   * @maxItems 1
   */
  route_ids?: [string];
  /**
   * Explicitly keep automatic delivery running without a per-session count cap. Pause/Stop, route timing and Telegram-required waits still apply. Omitted or false keeps max_auto_deliveries, including zero disabling sends.
   */
  unlimited_auto_deliveries?: boolean;
}
/**
 * Routing emits plan_id, contributing route_refs and destination revision/audience. Legacy DTOs without these are display/evaluation only, never approval or send authority.
 *
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "DeliveryProposal".
 */
export interface DeliveryProposal {
  post: ObservedPost;
  route: Route;
  /**
   * @maxItems 100
   */
  labels: string[];
  approval_required: boolean;
  plan_id?: string;
  /**
   * @minItems 1
   * @maxItems 100
   */
  route_refs?: [RouteReference, ...RouteReference[]];
  destination?: DestinationSnapshot;
  destination_version?: number;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "DestinationSnapshot".
 */
export interface DestinationSnapshot {
  account_id: TelegramId;
  chat_id: TelegramId;
  title: string;
  audience: "public" | "private" | "unknown";
  permissions: DestinationPermissions;
  sender: SenderIdentity;
  paid_required: boolean;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "DestinationPermissions".
 */
export interface DestinationPermissions {
  can_send: boolean;
  can_select_sender: boolean;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "AuthorizationResult".
 */
export interface AuthorizationResult {
  allowed: boolean;
  reason: string | null;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "ModelManifest".
 */
export interface ModelManifest {
  id: string;
  version: string;
  url: string;
  sha256: string;
  license: string;
  runtime_version: string;
  quantization: string;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "VerificationResult".
 */
export interface VerificationResult {
  valid: boolean;
  reason: string | null;
}
/**
 * Remaining names are diagnostics, never process handles or renderer-supplied control targets.
 *
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "ShutdownReport".
 */
export interface ShutdownReport {
  stopped: boolean;
  /**
   * @maxItems 100
   */
  remaining: string[];
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "PruneReport".
 */
export interface PruneReport {
  deleted_counts: {
    [k: string]: number;
  };
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "RecoverySummary".
 */
export interface RecoverySummary {
  counts: {
    [k: string]: number;
  };
  /**
   * @maxItems 1000
   */
  reasons: string[];
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "DeliveryEvent".
 */
export interface DeliveryEvent {
  delivery_id: string;
  state: DeliveryState;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "DatasetRecord".
 */
export interface DatasetRecord {
  post: ObservedPost;
  /**
   * @maxItems 100
   */
  expected_topic_ids: string[];
  language: "zh" | "en" | "mixed" | "other";
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "Dataset".
 */
export interface Dataset {
  /**
   * @maxItems 100000
   */
  records: DatasetRecord[];
  split: "train" | "validation" | "test";
  /**
   * @minItems 1
   * @maxItems 100
   */
  topics?: [Topic, ...Topic[]];
  /**
   * Required by the offline evaluation API. Synthetic data never supplies release evidence.
   */
  synthetic?: boolean;
}
/**
 * Metrics are report data only, never classifier actions or routing input.
 *
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "EvaluationReport".
 */
export interface EvaluationReport {
  metrics: {
    [k: string]: unknown;
  };
  versions: {
    [k: string]: string;
  };
  hardware: {
    [k: string]: string;
  };
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "EmptyPayload".
 */
export interface EmptyPayload {}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "DismissPayload".
 */
export interface DismissPayload {
  post_id: string;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "ErrorPayload".
 */
export interface ErrorPayload {
  code: "unsupported" | "invalid_request" | "internal_error";
  message: string;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "LogoutRequest".
 */
export interface LogoutRequest {
  mode: "close_local" | "server_logout" | "remove_local_offline";
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "DestinationRequest".
 */
export interface DestinationRequest {
  chat_id: TelegramId;
  kind: "private" | "group";
  sender: SenderIdentity | null;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "SenderRequest".
 */
export interface SenderRequest {
  chat_id: TelegramId;
  sender: SenderIdentity;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "ReviewApprovalRequest".
 */
export interface ReviewApprovalRequest {
  proposal: DeliveryProposal;
  disclosure: ReviewDisclosure;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "ReviewDisclosure".
 */
export interface ReviewDisclosure {
  plan_id: string;
  destination_version: number;
  destination_id: TelegramId;
  title: string;
  audience: "public" | "private" | "unknown";
  source_visibility: Visibility;
  sender_kind: "user" | "chat";
  sender_id: TelegramId;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "DismissReviewRequest".
 */
export interface DismissReviewRequest {
  plan_id: string;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "QrRequest".
 */
export interface QrRequest {
  challenge_id: string;
  account_epoch: AccountEpoch;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "RetentionPolicy".
 */
export interface RetentionPolicy {
  text_days: number;
  dedup_days: number;
  diagnostic_days: number;
  confirmed: boolean;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "EvaluatedAutoPolicy".
 */
export interface EvaluatedAutoPolicy {
  confirmed: true;
  min_precision: number;
  min_recall: number;
  min_positive: number;
  min_predicted: number;
  /**
   * @minItems 1
   * @maxItems 4
   */
  languages:
    | ["en" | "zh" | "mixed" | "other"]
    | ["en" | "zh" | "mixed" | "other", "en" | "zh" | "mixed" | "other"]
    | ["en" | "zh" | "mixed" | "other", "en" | "zh" | "mixed" | "other", "en" | "zh" | "mixed" | "other"]
    | [
        "en" | "zh" | "mixed" | "other",
        "en" | "zh" | "mixed" | "other",
        "en" | "zh" | "mixed" | "other",
        "en" | "zh" | "mixed" | "other"
      ];
  method?: "evaluated";
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "AutoRouteOptIn".
 */
export interface AutoRouteOptIn {
  route: Route;
  confirmed: true;
  expected_receipt: AutoPreviewReceipt;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "SampleRequest".
 */
export interface SampleRequest {
  text: string;
  post_id?: string;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "BulkReviewRequest".
 */
export interface BulkReviewRequest {
  /**
   * @minItems 1
   * @maxItems 20
   */
  items:
    | [ReviewApprovalRequest]
    | [ReviewApprovalRequest, ReviewApprovalRequest]
    | [ReviewApprovalRequest, ReviewApprovalRequest, ReviewApprovalRequest]
    | [ReviewApprovalRequest, ReviewApprovalRequest, ReviewApprovalRequest, ReviewApprovalRequest]
    | [
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest
      ]
    | [
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest
      ]
    | [
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest
      ]
    | [
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest
      ]
    | [
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest
      ]
    | [
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest
      ]
    | [
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest
      ]
    | [
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest
      ]
    | [
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest
      ]
    | [
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest
      ]
    | [
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest
      ]
    | [
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest
      ]
    | [
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest
      ]
    | [
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest
      ]
    | [
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest
      ]
    | [
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest,
        ReviewApprovalRequest
      ];
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "BulkDismissRequest".
 */
export interface BulkDismissRequest {
  /**
   * @minItems 1
   * @maxItems 20
   */
  items:
    | [DismissReviewRequest]
    | [DismissReviewRequest, DismissReviewRequest]
    | [DismissReviewRequest, DismissReviewRequest, DismissReviewRequest]
    | [DismissReviewRequest, DismissReviewRequest, DismissReviewRequest, DismissReviewRequest]
    | [DismissReviewRequest, DismissReviewRequest, DismissReviewRequest, DismissReviewRequest, DismissReviewRequest]
    | [
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest
      ]
    | [
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest
      ]
    | [
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest
      ]
    | [
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest
      ]
    | [
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest
      ]
    | [
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest
      ]
    | [
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest
      ]
    | [
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest
      ]
    | [
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest
      ]
    | [
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest
      ]
    | [
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest
      ]
    | [
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest
      ]
    | [
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest
      ]
    | [
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest
      ]
    | [
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest,
        DismissReviewRequest
      ];
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "ArchivePostsQuery".
 */
export interface ArchivePostsQuery {
  cursor?: string | null;
  limit?: number;
  search?: string;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "ArchiveRunsQuery".
 */
export interface ArchiveRunsQuery {
  cursor?: string | null;
  limit?: number;
  revision_id?: string | null;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "ArchiveDetailRequest".
 */
export interface ArchiveDetailRequest {
  kind: "revision" | "run" | "delivery";
  record_id: string;
  offset: number;
  expected_digest?: string;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "SaveMessageTemplateRequest".
 */
export interface SaveMessageTemplateRequest {
  id: string | null;
  expected_version: number | null;
  name: string;
  body: string;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "ConfigureDeliveryDraftRequest".
 */
export interface ConfigureDeliveryDraftRequest {
  id: string | null;
  /**
   * @maxItems 50
   */
  revision_ids: string[];
  target: WorkflowTarget | null;
  template_id: string;
  template_version: number;
  timing: DeliveryTiming;
  expected_version: number | null;
  source_policy?: DeliverySourcePolicy;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "WorkflowTarget".
 */
export interface WorkflowTarget {
  account_id: TelegramId;
  account_epoch: AccountEpoch;
  destination_id: TelegramId;
  destination_version: number;
  sender: SenderIdentity;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "GetDeliveryDraftRequest".
 */
export interface GetDeliveryDraftRequest {
  draft_id: string;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "PreviewDeliveryDraftRequest".
 */
export interface PreviewDeliveryDraftRequest {
  draft_id: string;
  version: number;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "ApproveDeliveryDraftRequest".
 */
export interface ApproveDeliveryDraftRequest {
  draft_id: string;
  version: number;
  preview_hash: string;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "DeliveryJobsQuery".
 */
export interface DeliveryJobsQuery {
  cursor: string | null;
  limit: number;
  status: DeliveryJobStatus | null;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "DeliveryJobRequest".
 */
export interface DeliveryJobRequest {
  job_id: string;
  version: number;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "TelegramDestinationsQuery".
 */
export interface TelegramDestinationsQuery {
  cursor?: string | null;
  limit?: number;
  search?: string;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "TelegramDestinationsRefresh".
 */
export interface TelegramDestinationsRefresh {
  load_more: boolean;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "ActivityQuery".
 */
export interface ActivityQuery {
  after?: string | null;
  before?: string | null;
  limit?: number;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "TelegramTestRequest".
 */
export interface TelegramTestRequest {
  chat_id: TelegramId;
  account_id: TelegramId;
  account_epoch: AccountEpoch;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "SaveTelegramDestinationRequest".
 */
export interface SaveTelegramDestinationRequest {
  chat_id: TelegramId;
  account_id: TelegramId;
  account_epoch: AccountEpoch;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "ManualHistorySendRequest".
 */
export interface ManualHistorySendRequest {
  job_id: string;
  version: number;
  preview_receipt: string;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "RouteQueueQuery".
 */
export interface RouteQueueQuery {
  route_id?: string;
  limit?: number;
  cursor?: string | null;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "RouteQueuePauseRequest".
 */
export interface RouteQueuePauseRequest {
  route_id: string;
  paused: boolean;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "RouteQueueCancelRequest".
 */
export interface RouteQueueCancelRequest {
  delivery_id: string;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "HistoryItem".
 */
export interface HistoryItem {
  delivery: Delivery;
  /**
   * @maxItems 10000
   */
  attempts: TelegramAttempt[];
}
/**
 * Durable ordered own-send correlation only. Initial temporary message is not Sent; no API response, message content or secret fields. request_extra correlates @extra and is not server idempotency.
 *
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "TelegramAttempt".
 */
export interface TelegramAttempt {
  delivery_id: string;
  attempt_id: string;
  request_extra: string;
  temporary_message_id: TelegramId | null;
  final_message_id: TelegramId | null;
  state: DeliveryState;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "ReviewItem".
 */
export interface ReviewItem {
  proposal: DeliveryProposal;
  outgoing_text: string;
  classification: Classification | null;
  /**
   * @maxItems 100
   */
  topics: TopicSummary[];
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "TopicSummary".
 */
export interface TopicSummary {
  id: string;
  version: number;
  description: string;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "AccountView".
 */
export interface AccountView {
  snapshot: AccountSnapshot;
  challenge: AuthChallenge | null;
  status:
    | "idle"
    | "authorizing"
    | "ready"
    | "authentication_error"
    | "unsupported_auth_state"
    | "authentication_unavailable"
    | "closed"
    | "closing"
    | "revocation_pending"
    | "account_unavailable"
    | "locally_removed"
    | "server_revoked"
    | "cleanup_pending"
    | "challenge_expired";
  /**
   * @maxItems 10000
   */
  pending_revocations: TelegramId[];
}
/**
 * Application status only, never reusable secrets, phone/code/password or raw TDLib updates. Does not construct a TDLib client or grant network permission.
 *
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "AccountSnapshot".
 */
export interface AccountSnapshot {
  account_id: TelegramId | null;
  epoch: AccountEpoch;
  auth_state: "logged_out" | "authorizing" | "ready" | "closed" | "unknown";
  display_name: string | null;
  network_state: "offline" | "connecting" | "online";
  revocation_pending: boolean;
}
/**
 * Opaque challenge metadata only; secure submission operations and state transitions belong to account onboarding, not generic IPC.
 *
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "AuthChallenge".
 */
export interface AuthChallenge {
  kind: "phone" | "code" | "password" | "qr";
  challenge_id: string;
  expires_at: string | null;
  code_delivery?: AuthCodeDelivery | null;
}
/**
 * Allowlisted delivery channel and server wait only. Never a phone number, code, password, URL or raw native payload.
 *
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "AuthCodeDelivery".
 */
export interface AuthCodeDelivery {
  method: "telegram" | "sms" | "call" | "fragment" | "unknown";
  next_method: ("telegram" | "sms" | "call" | "fragment" | "unknown") | null;
  resend_after_seconds: number | null;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "Acknowledgment".
 */
export interface Acknowledgment {
  operation:
    | "SaveTopic"
    | "SaveRoute"
    | "ApproveReview"
    | "DismissReview"
    | "CancelTelegramLogin"
    | "LogoutTelegram"
    | "OpenThreadsLogin"
    | "BeginTelegramLogin"
    | "VerifyDestination"
    | "SelectSender"
    | "RequestTelegramQr"
    | "SaveRetention"
    | "SaveAutoPolicy"
    | "OptInAutoRoute"
    | "VerifySavedMessages"
    | "RefreshTelegramDestinations"
    | "SendTelegramTest"
    | "SaveTelegramDestination"
    | "SendManualHistoryOnce"
    | "SetRouteQueuePaused"
    | "CancelRouteQueueItem"
    | "RestoreTelegramSession"
    | "CheckTelegramLogin"
    | "ResendTelegramCode"
    | "ClearRouteQueue";
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "DestinationStatus".
 */
export interface DestinationStatus {
  state: "idle" | "pending" | "verified" | "sender_selected" | "failed";
  snapshot: DestinationSnapshot | null;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "Readiness".
 */
export interface Readiness {
  mode: Mode;
  browser: "ready" | "unavailable";
  inference: "ready" | "unavailable";
  telegram: "ready" | "unavailable";
  automatic: "ready" | "unavailable";
  can_start: boolean;
  reason:
    | (
        | "browser_unavailable"
        | "inference_unavailable"
        | "telegram_unavailable"
        | "automatic_approval_unavailable"
        | "select_one_auto_route"
        | "selected_auto_route_unavailable"
        | "automatic_outcome_unknown"
        | "telegram_connection_required"
      )
    | null;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "AutoStatus".
 */
export interface AutoStatus {
  policy: AutoPolicy | null;
  evidence_available: boolean;
  eligible: boolean;
  reason: string;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "SampleResult".
 */
export interface SampleResult {
  classification: Classification;
  sending_possible: false;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "BulkReviewResult".
 */
export interface BulkReviewResult {
  /**
   * @maxItems 20
   */
  results:
    | []
    | [
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        }
      ]
    | [
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        }
      ]
    | [
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        }
      ]
    | [
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        }
      ]
    | [
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        }
      ]
    | [
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        }
      ]
    | [
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        }
      ]
    | [
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        }
      ]
    | [
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        }
      ]
    | [
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        }
      ]
    | [
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        }
      ]
    | [
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        }
      ]
    | [
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        }
      ]
    | [
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        }
      ]
    | [
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        }
      ]
    | [
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        }
      ]
    | [
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        }
      ]
    | [
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        }
      ]
    | [
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        }
      ]
    | [
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        },
        {
          plan_id: string;
          status: "staged" | "dismissed" | "rejected";
        }
      ];
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "AutoRoutePreview".
 */
export interface AutoRoutePreview {
  route: Route;
  destination: DestinationSnapshot;
  destination_version: number;
  policy: AutoPolicy;
  evidence_sha256: AutoPreviewReceipt | null;
  /**
   * @maxItems 100
   */
  topics: Topic[];
  versions: {
    [k: string]: string;
  };
  receipt: AutoPreviewReceipt;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "CollectionItem".
 */
export interface CollectionItem {
  post: ObservedPost;
  classification: Classification | null;
  /**
   * @maxItems 100
   */
  topics: {
    id: string;
    label: string;
  }[];
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "ArchivePostsPage".
 */
export interface ArchivePostsPage {
  /**
   * @maxItems 100
   */
  items: ArchiveRevision[];
  next_cursor: string | null;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "ArchiveRevision".
 */
export interface ArchiveRevision {
  revision_id: string;
  post_id: string;
  permalink: string;
  author: string;
  text_preview: string;
  observed_at: string;
  visibility: Visibility;
  /**
   * @maxItems 100
   */
  quality_flags: ExtractionIssue[];
  provenance: "captured" | "legacy_import";
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "ArchiveRunsPage".
 */
export interface ArchiveRunsPage {
  /**
   * @maxItems 100
   */
  items: ArchiveRun[];
  next_cursor: string | null;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "ArchiveRun".
 */
export interface ArchiveRun {
  run_id: string;
  kind: "collection" | "sample" | "legacy";
  revision_id: string | null;
  started_at: string | null;
  finished_at: string | null;
  status: "running" | "succeeded" | "failed" | "cancelled";
  classification: Classification | null;
  error_code: string | null;
  input_preview: string;
  topic_count: number;
  binding: {
    [k: string]: string | number | boolean;
  };
  provenance: "captured" | "legacy_import";
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "ArchiveDetailChunk".
 */
export interface ArchiveDetailChunk {
  kind: "revision" | "run" | "delivery";
  record_id: string;
  offset: number;
  data: string;
  next_offset: number | null;
  digest: string;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "DeliveryWorkspace".
 */
export interface DeliveryWorkspace {
  /**
   * @maxItems 100
   */
  templates: MessageTemplate[];
  /**
   * @maxItems 100
   */
  targets: {
    target: WorkflowTarget;
    destination: DestinationSnapshot;
  }[];
  /**
   * @maxItems 50
   */
  drafts: DeliveryDraft[];
  /**
   * @maxItems 100
   */
  setup_reasons: string[];
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "MessageTemplate".
 */
export interface MessageTemplate {
  id: string;
  version: number;
  name: string;
  body: string;
  readonly: boolean;
  created_at: string;
  updated_at: string;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "DeliveryDraft".
 */
export interface DeliveryDraft {
  id: string;
  version: number;
  /**
   * @maxItems 50
   */
  revision_ids: string[];
  target: WorkflowTarget | null;
  template_id: string;
  template_version: number;
  timing: DeliveryTiming;
  created_at: string;
  updated_at: string;
  source_policy: DeliverySourcePolicy;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "MessageTemplateResult".
 */
export interface MessageTemplateResult {
  template: MessageTemplate;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "DeliveryDraftResult".
 */
export interface DeliveryDraftResult {
  draft: DeliveryDraft;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "DeliveryPreview".
 */
export interface DeliveryPreview {
  draft_id: string;
  version: number;
  preview_hash: string;
  /**
   * @maxItems 50
   */
  items: DeliveryPreviewItem[];
  target: WorkflowTarget | null;
  timing: DeliveryTiming;
  approvable: boolean;
  /**
   * @maxItems 100
   */
  blocked_reasons: string[];
  destination: DestinationSnapshot | null;
  /**
   * @maxItems 100
   */
  topic_bindings: {
    id: string;
    version: number;
    description: string;
  }[];
  source_policy: DeliverySourcePolicy;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "DeliveryPreviewItem".
 */
export interface DeliveryPreviewItem {
  revision_id: string;
  post_id: string;
  url: string;
  text: string;
  due_at: string;
  /**
   * @maxItems 100
   */
  missing_fields: string[];
  source_eligible: boolean;
  /**
   * @maxItems 100
   */
  blocked_reasons: string[];
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "DeliveryJobs".
 */
export interface DeliveryJobs {
  /**
   * @maxItems 50
   */
  items: DeliveryJob[];
  next_cursor: string | null;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "DeliveryJob".
 */
export interface DeliveryJob {
  id: string;
  version: number;
  kind: "job" | "rule";
  draft_id: string;
  draft_version: number;
  revision_id: string | null;
  post_id: string | null;
  text: string | null;
  target: WorkflowTarget | null;
  due_at: string | null;
  timing: DeliveryTiming;
  status: DeliveryJobStatus;
  /**
   * @maxItems 100
   */
  blocked_reasons: string[];
  requires_approval: boolean;
  rule_id: string | null;
  delivery_id: string | null;
  attempt_id: string | null;
  created_at: string;
  updated_at: string;
  source_policy: DeliverySourcePolicy;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "DeliveryJobResult".
 */
export interface DeliveryJobResult {
  job: DeliveryJob;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "ArchiveDeliveriesPage".
 */
export interface ArchiveDeliveriesPage {
  /**
   * @maxItems 100
   */
  items: ArchiveDelivery[];
  next_cursor: string | null;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "ArchiveDelivery".
 */
export interface ArchiveDelivery {
  delivery: Delivery;
  attempt_count: number;
  created_at: string;
  updated_at: string;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "AppStatus".
 */
export interface AppStatus {
  service: "ready";
  model: ModelRuntimeStatus;
  activity: "collection" | "sample" | null;
  session: SessionSnapshot;
  recovery?: RecoverySnapshot | null;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "ModelRuntimeStatus".
 */
export interface ModelRuntimeStatus {
  state: "not_loaded" | "starting" | "ready" | "classifying" | "stopping" | "unavailable";
  model_id: string | null;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "RecoverySnapshot".
 */
export interface RecoverySnapshot {
  reason: "unsupported_cards" | "unknown_layout" | "empty_feed";
  retry_in_seconds: number;
  attempt: number;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "TelegramDestinations".
 */
export interface TelegramDestinations {
  state: "idle" | "loading" | "ready" | "blocked" | "failed";
  account_id: string | null;
  account_epoch: number | null;
  /**
   * @maxItems 50
   */
  items: TelegramDestinationCandidate[];
  next_cursor: string | null;
  can_load_more: boolean;
  exhausted: boolean;
  capped: boolean;
  failure_stage?: "load" | "chats" | "chat" | "member" | null;
  failure_reason?: "timeout" | "invalid_response" | "request_unavailable" | null;
  blocked_reason: "login_required" | "connection_required" | "native_unavailable" | "refresh_required" | null;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "TelegramDestinationCandidate".
 */
export interface TelegramDestinationCandidate {
  chat_id: string;
  title: string;
  kind: "saved_messages" | "private" | "group" | "channel" | "secret" | "unsupported";
  can_verify: boolean;
  sender: SenderIdentity | null;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "ActivityPage".
 */
export interface ActivityPage {
  /**
   * @maxItems 100
   */
  entries: ActivityEntry[];
  cursor: string;
  has_more: boolean;
  older_cursor: string | null;
  as_of: string;
  session: SessionSnapshot;
  model: ModelRuntimeStatus;
  recording_ok: boolean;
  recovery?: RecoverySnapshot | null;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "ActivityEntry".
 */
export interface ActivityEntry {
  sequence: string;
  at: string;
  category: "session" | "collection" | "model" | "delivery";
  kind:
    | "session_starting"
    | "session_running"
    | "session_paused"
    | "session_attention"
    | "session_stopped"
    | "reading"
    | "skipped"
    | "history_saved"
    | "classification_started"
    | "classification_succeeded"
    | "classification_failed"
    | "classification_cancelled"
    | "delivery_pending"
    | "delivery_preparing"
    | "delivery_sending"
    | "delivery_sent"
    | "delivery_failed"
    | "delivery_unknown"
    | "delivery_cancelled"
    | "delivery_retryable"
    | "job_draft"
    | "job_waiting"
    | "job_blocked"
    | "job_paused"
    | "job_cancelled"
    | "collection_recovering"
    | "collection_recovered"
    | "telegram_test_verifying"
    | "telegram_test_sending"
    | "telegram_test_sent"
    | "telegram_test_failed"
    | "telegram_test_unknown";
  revision_id: string | null;
  run_id: string | null;
  delivery_id: string | null;
  job_id: string | null;
  author: string | null;
  text_preview: string | null;
  permalink: string | null;
  destination: string | null;
  detail: string | null;
  result: null | "match" | "no_match" | "abstained";
  due_at: string | null;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "TelegramTestState".
 */
export interface TelegramTestState {
  state: "idle" | "verifying" | "sending" | "closing" | "sent" | "failed" | "unknown";
  chat_id: TelegramId | null;
  account_id: TelegramId | null;
  account_epoch: AccountEpoch | null;
  text: string | null;
  request_extra: string | null;
  temporary_message_id: TelegramId | null;
  final_message_id: TelegramId | null;
  reason: string | null;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "SavedTelegramDestinations".
 */
export interface SavedTelegramDestinations {
  /**
   * @maxItems 200
   */
  items: SavedTelegramDestination[];
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "SavedTelegramDestination".
 */
export interface SavedTelegramDestination {
  chat_id: TelegramId;
  title: string;
  kind: "private" | "group" | "channel" | "saved_messages";
  sender: SenderIdentity;
  account_id: TelegramId;
  account_epoch: AccountEpoch;
  saved_at: string;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "ManualHistoryPreview".
 */
export interface ManualHistoryPreview {
  job_id: string;
  job_version: number;
  revision_id: string;
  text: string;
  destination: DestinationSnapshot;
  target: WorkflowTarget;
  preview_receipt: string;
  expires_at: string;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "ManualHistorySendState".
 */
export interface ManualHistorySendState {
  state: "idle" | "verifying" | "sending" | "closing" | "sent" | "failed" | "unknown";
  job_id: string | null;
  job_version: number | null;
  reason:
    | null
    | "preflight_unavailable"
    | "verification_failed"
    | "authority_changed"
    | "dispatch_unconfirmed"
    | "close_unconfirmed"
    | "durability_unconfirmed"
    | "timed_out";
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "RouteQueue".
 */
export interface RouteQueue {
  /**
   * @maxItems 200
   */
  items: RouteQueueItem[];
  paused: boolean;
  next_cursor: string | null;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "RouteQueueItem".
 */
export interface RouteQueueItem {
  id: string;
  route_id: string;
  route_version: number;
  text: string;
  destination_id: TelegramId;
  destination_title: string;
  due_at: string | null;
  state: "queued" | "waiting" | "sending" | "sent" | "failed" | "unknown" | "cancelled" | "blocked";
  reason: string | null;
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "TelegramRecoveryState".
 */
export interface TelegramRecoveryState {
  state: "closing" | "ready_to_login" | "blocked" | "failed";
  reason?:
    | "not_started"
    | "recovery_interrupted"
    | "delivery_not_unknown"
    | "account_changed"
    | "attempt_changed"
    | "profile_binding_changed"
    | "profile_unavailable"
    | "quarantine_unavailable"
    | "native_client_not_closed"
    | "quarantine_verification_failed";
}
/**
 * This interface was referenced by `ThreadRelayProtocolV1`'s JSON-Schema
 * via the `definition` "ArchiveRunDetail".
 */
export interface ArchiveRunDetail {
  run_id: string;
  kind: "collection" | "sample" | "legacy";
  revision_id: string | null;
  started_at: string | null;
  finished_at: string | null;
  status: "running" | "succeeded" | "failed" | "cancelled";
  classification: Classification | null;
  error_code: string | null;
  input_preview: string;
  topic_count: number;
  binding: {
    [k: string]: string | number | boolean;
  };
  provenance: "captured" | "legacy_import";
  input_snapshot: {
    post?: ObservedPost;
    text?: string;
  };
  /**
   * @maxItems 100
   */
  topics: Topic[];
}

import * as compiledValidators from './contracts.validators.mjs';

// Types alone do not validate untrusted JSON. These functions were compiled at
// build time, with no renderer code generation, coercion or default mutation.
const validators: Readonly<Record<string, (value: unknown) => boolean>> = compiledValidators;
export function validateContract(name: string, value: unknown): boolean {
  try { return Object.hasOwn(validators, name) && validators[name](value) === true; }
  catch { return false; }
}
export function isCommand(value: unknown): value is Command { return validateContract('Command', value); }
export function isEvent(value: unknown): value is Event { return validateContract('Event', value); }
