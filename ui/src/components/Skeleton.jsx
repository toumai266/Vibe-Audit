import './Skeleton.css';

export function SkeletonLine({ width = '100%', height = '14px' }) {
  return <div className="skeleton-line" style={{ width, height }} />;
}

export function SkeletonCard() {
  return (
    <div className="skeleton-card">
      <div className="skeleton-card-header">
        <SkeletonLine width="120px" height="13px" />
        <SkeletonLine width="50px" height="18px" />
      </div>
      <SkeletonLine width="80px" height="13px" />
      <SkeletonLine width="60px" height="12px" />
    </div>
  );
}

export function SessionListSkeleton() {
  return (
    <div className="skeleton-list">
      <SkeletonCard />
      <SkeletonCard />
      <SkeletonCard />
      <SkeletonCard />
    </div>
  );
}

export function SessionDetailSkeleton() {
  return (
    <div className="skeleton-detail">
      <div className="skeleton-detail-header">
        <SkeletonLine width="200px" height="16px" />
        <div className="skeleton-detail-actions">
          <SkeletonLine width="60px" height="30px" />
          <SkeletonLine width="60px" height="30px" />
        </div>
      </div>
      <div className="skeleton-detail-body">
        <div className="skeleton-baseline">
          <SkeletonLine width="160px" height="12px" />
          <SkeletonLine width="100%" height="14px" />
          <SkeletonLine width="80%" height="14px" />
        </div>
        <div className="skeleton-turns">
          <div className="skeleton-turn">
            <SkeletonLine width="80px" height="12px" />
            <SkeletonLine width="100%" height="14px" />
            <SkeletonLine width="70%" height="14px" />
          </div>
          <div className="skeleton-turn">
            <SkeletonLine width="80px" height="12px" />
            <SkeletonLine width="100%" height="14px" />
            <SkeletonLine width="90%" height="14px" />
            <SkeletonLine width="50%" height="14px" />
          </div>
          <div className="skeleton-turn">
            <SkeletonLine width="80px" height="12px" />
            <SkeletonLine width="100%" height="14px" />
          </div>
        </div>
      </div>
    </div>
  );
}

export function TimelineSkeleton() {
  return (
    <div className="skeleton-timeline">
      <div className="skeleton-baseline">
        <SkeletonLine width="160px" height="12px" />
        <SkeletonLine width="100%" height="14px" />
        <SkeletonLine width="80%" height="14px" />
      </div>
      <div className="skeleton-turns">
        <div className="skeleton-turn">
          <SkeletonLine width="80px" height="12px" />
          <SkeletonLine width="100%" height="14px" />
          <SkeletonLine width="70%" height="14px" />
        </div>
        <div className="skeleton-turn">
          <SkeletonLine width="80px" height="12px" />
          <SkeletonLine width="100%" height="14px" />
          <SkeletonLine width="90%" height="14px" />
        </div>
        <div className="skeleton-turn">
          <SkeletonLine width="80px" height="12px" />
          <SkeletonLine width="100%" height="14px" />
        </div>
      </div>
    </div>
  );
}
