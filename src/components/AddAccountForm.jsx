import { useState } from "react";

export default function AddAccountForm() {
  const [operatorKey, setOperatorKey] = useState("");
  const [tradingKey, setTradingKey] = useState("");
  const [message, setMessage] = useState("");

  const handleSubmit = async (e) => {
    e.preventDefault();
    const res = await fetch("https://cantexback.onrender.com/add_account", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ operator_key: operatorKey, trading_key: tradingKey }),
    });
    const data = await res.json();
    setMessage(data.message || "Account added!");
  };

  return (
    <form onSubmit={handleSubmit}>
      <h2>Add Account</h2>
      <input
        type="text"
        placeholder="Operator Key"
        value={operatorKey}
        onChange={(e) => setOperatorKey(e.target.value)}
      />
      <input
        type="text"
        placeholder="Trading Key"
        value={tradingKey}
        onChange={(e) => setTradingKey(e.target.value)}
      />
      <button type="submit">Add Account</button>
      {message && <p>{message}</p>}
    </form>
  );
}