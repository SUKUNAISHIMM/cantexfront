import { useEffect, useState } from "react";

export default function AccountList() {
  const [accounts, setAccounts] = useState([]);

  useEffect(() => {
    fetch("https://cantexback.onrender.com/accounts")
      .then(res => res.json())
      .then(data => setAccounts(data))
      .catch(() => setAccounts([]));
  }, []);

  const removeAccount = async (id) => {
    await fetch("https://cantexback.onrender.com/remove_account", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id }),
    });
    setAccounts(accounts.filter(acc => acc.id !== id));
  };

  return (
    <div>
      <h2>Accounts</h2>
      <ul>
        {accounts.map(acc => (
          <li key={acc.id} className="flex justify-between">
            <span>{acc.operator_key.slice(0, 10)}...</span>
            <button onClick={() => removeAccount(acc.id)}>Remove</button>
          </li>
        ))}
      </ul>
    </div>
  );
}