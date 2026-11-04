const labels: Record<string, string> = {stopped:'Stopped',starting:'Starting',running:'Running',pausing:'Pausing',paused:'Paused',stopping:'Stopping',needs_attention:'Needs attention',failed:'Failed',pending:'Pending',sending:'Sending',sent:'Sent',retryable:'Retryable',unknown:'Unknown',cancelled:'Cancelled',review:'Review',dry_run:'Dry run',auto:'Auto-send'};
export function StatusBadge({value}: {value: string}) {
  const tone = value === 'sent' ? 'success' : value === 'failed' ? 'danger' : ['unknown','retryable','needs_attention'].includes(value) ? 'warning' : value === 'running' ? 'active' : 'neutral';
  return <span className={`badge badge-${tone}`}>{value === 'unknown' ? '⚠ ' : ''}{labels[value] ?? value}</span>;
}
