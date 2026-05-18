import FeeWidget from "./components/FeeWidget";
import AddAccountForm from "./components/AddAccountForm";
import AccountList from "./components/AccountList";

export default function App() {
  return (
    <div className="p-6">
      <h1>Sukuna Swap Bot · Command Center</h1>
      <div className="panel">
        <FeeWidget />
      </div>
      <div className="panel">
        <AddAccountForm />
      </div>
      <div className="panel">
        <AccountList />
      </div>
    </div>
  );
}