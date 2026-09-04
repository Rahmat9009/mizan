import { Link } from 'react-router-dom';

export function NotFound() {
  return (
    <div className="notfound">
      <p className="u-eyebrow">404</p>
      <h1>No view at this address.</h1>
      <p>The route does not exist. The Command Center is the place to start.</p>
      <Link to="/app">Open the Command Center</Link>
    </div>
  );
}
