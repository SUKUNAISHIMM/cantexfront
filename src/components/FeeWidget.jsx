import { useEffect, useState } from "react";

export default function FeeWidget() {
  const [fee, setFee] = useState(null);

  useEffect(() => {
    fetch("https://cantexback.onrender.com/current_fee")
      .then(res => res.json())
      .then(data => setFee(data.fee))
      .catch(() => setFee("Error"));
  }, []);

  return (
    <div>
      <h2>Network Fee</h2>
      <p>{fee !== null ? fee : "Loading..."}</p>
    </div>
  );
}