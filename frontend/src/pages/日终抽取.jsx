import React, { useEffect, useState } from "react";
import { api } from "../api.js";

export default function 日终抽取() {
  const [日, set日] = useState("2026-11-20");
  const [批, set批] = useState([]);
  const [证, set证] = useState(null);
  const [讯, set讯] = useState("");

  const 刷新 = () => Promise.all([api.抽取批次(), api.切日核证()]).then(([b, z]) => {
    set批(b);
    set证(z);
  });

  useEffect(() => {
    刷新().catch((e) => set讯(String(e.message || e)));
  }, []);

  return (
    <div>
      <h1 className="题">日终抽取</h1>
      <p className="述">
        按营业日写周期快照：前日余额 + 当日流水净额。抽取批须先入切日 FIFO 取得租约（口径乙·配对租约排队），
        租约到期即失败闭并回滚，禁止半截日镜。
      </p>
      <div className="提示条">
        切日核证：{证?.valid ? `持有方=${证.持有方}，租约截止=${Number(证.租约截止).toFixed(1)}，valid=true` : "无有效租约（空闲）"}
        {typeof 证?.排队等待数 === "number" ? `；排队等待=${证.排队等待数}` : ""}
      </div>
      <div className="行">
        <input value={日} onChange={(e) => set日(e.target.value)} placeholder="YYYY-MM-DD" />
        <button
          onClick={() =>
            api
              .抽取(日)
              .then((r) => set讯(`批次 ${r.批次编号} 写入=${r.写入} 盖写=${r.盖写} 断档=${r.断档} 租约=${r.租约号?.slice(0, 10)}…`))
              .then(刷新)
              .catch((e) => set讯(String(e.message || e)))
          }
        >
          执行抽取（持租约）
        </button>
        <button className="次" onClick={() => 刷新().catch((e) => set讯(String(e.message || e)))}>刷新批次</button>
      </div>
      {讯 ? <p className="讯">{讯}</p> : null}
      <div className="段">
        <h2 className="段题">抽取批次（同票租约号 / 失败闭可见）</h2>
        {!批.length ? (
          <div className="空态">尚无抽取</div>
        ) : (
          <table>
            <thead>
              <tr>
                <th>编号</th>
                <th>营业日</th>
                <th>时刻</th>
                <th>状态</th>
                <th>租约号</th>
                <th>报告</th>
              </tr>
            </thead>
            <tbody>
              {批.map((r) => (
                <tr key={r.编号}>
                  <td>{r.编号}</td>
                  <td>{r.营业日}</td>
                  <td>{r.时刻}</td>
                  <td>
                    <span className={`徽章 ${r.状态 === "成功" ? "通" : "败"}`}>{r.状态 || "成功"}</span>
                  </td>
                  <td>{r.租约号 ? r.租约号.slice(0, 12) + "…" : "—"}</td>
                  <td>{typeof r.报告 === "object" ? JSON.stringify(r.报告) : r.报告}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
