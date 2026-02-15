import { Component } from 'react';

export default class ErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error) {
    return { hasError: true, error };
  }

  componentDidCatch(error, info) {
    console.error('ErrorBoundary caught:', error, info.componentStack);
  }

  render() {
    if (this.state.hasError) {
      return (
        <div style={{
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          height: '100%',
          padding: 40,
          color: 'var(--text-muted)',
          gap: 16,
        }}>
          <div style={{ fontSize: 32 }}>Something went wrong</div>
          <div style={{ fontSize: 14, color: 'var(--text-dim)', maxWidth: 500, textAlign: 'center' }}>
            {this.state.error?.message || 'An unexpected error occurred'}
          </div>
          <button
            className="btn"
            onClick={() => this.setState({ hasError: false, error: null })}
            style={{ marginTop: 8 }}
          >
            Try Again
          </button>
        </div>
      );
    }

    return this.props.children;
  }
}
