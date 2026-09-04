import { Route, Routes } from 'react-router-dom';
import { AppShell } from '@/components/shell/AppShell';
import { Agents } from '@/routes/app/Agents';
import { Audit } from '@/routes/app/Audit';
import { Crowding } from '@/routes/app/Crowding';
import { Dashboard } from '@/routes/app/Dashboard';
import { Orders } from '@/routes/app/Orders';
import { Portfolio } from '@/routes/app/Portfolio';
import { ProposalDetail } from '@/routes/app/ProposalDetail';
import { Proposals } from '@/routes/app/Proposals';
import { RiskCenter } from '@/routes/app/RiskCenter';
import { Settings } from '@/routes/app/Settings';
import { Landing } from '@/routes/landing/Landing';
import { NotFound } from '@/routes/NotFound';

export function App() {
  return (
    <Routes>
      <Route path="/" element={<Landing />} />
      <Route path="/app" element={<AppShell />}>
        <Route index element={<Dashboard />} />
        <Route path="proposals" element={<Proposals />} />
        <Route path="proposals/:proposalId" element={<ProposalDetail />} />
        <Route path="portfolio" element={<Portfolio />} />
        <Route path="risk" element={<RiskCenter />} />
        <Route path="crowding" element={<Crowding />} />
        <Route path="orders" element={<Orders />} />
        <Route path="audit" element={<Audit />} />
        <Route path="agents" element={<Agents />} />
        <Route path="settings" element={<Settings />} />
      </Route>
      <Route path="*" element={<NotFound />} />
    </Routes>
  );
}
