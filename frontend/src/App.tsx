import { Navigate, Route, Routes } from "react-router-dom";
import { useAuth } from "./auth";
import Layout from "./components/Layout";
import Audit from "./pages/Audit";
import Channels from "./pages/Channels";
import Configuration from "./pages/Configuration";
import CustomerDetail from "./pages/CustomerDetail";
import Customers from "./pages/Customers";
import Dashboard from "./pages/Dashboard";
import Engine from "./pages/Engine";
import Login from "./pages/Login";
import Preparation from "./pages/Preparation";
import Reports from "./pages/Reports";
import TransactionDetail from "./pages/TransactionDetail";
import Transactions from "./pages/Transactions";
import Users from "./pages/Users";

export default function App() {
  const { user, loading } = useAuth();
  if (loading) return <div className="splash">Loading…</div>;
  if (!user) return <Login />;
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Dashboard />} />
        <Route path="alerts" element={<Transactions alerts />} />
        <Route path="transactions" element={<Transactions />} />
        <Route path="transactions/:id" element={<TransactionDetail />} />
        <Route path="customers" element={<Customers />} />
        <Route path="customers/:id" element={<CustomerDetail />} />
        <Route path="reports" element={<Reports />} />
        <Route path="channels" element={<Channels />} />
        <Route path="preparation" element={<Preparation />} />
        <Route path="engine" element={<Engine />} />
        <Route path="configuration" element={<Configuration />} />
        <Route path="users" element={<Users />} />
        <Route path="audit" element={<Audit />} />
        <Route path="*" element={<Navigate to="/" />} />
      </Route>
    </Routes>
  );
}
