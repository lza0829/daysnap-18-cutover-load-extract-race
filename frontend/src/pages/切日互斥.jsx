import React, { useEffect, useState } from "react";
import { api } from "../api.js";

export default function 切日互斥() {
  const [核证, set核证] = useState(null);
  const [队, set队] = useState([]);
  const [批, set批] = useState([]);
  const [夹具们, set夹具们] = useState([]);
  const [切点日, set切点日] = useState("2026-11-25");
  const [讯, set讯] = useState("");
  const [果, set果] = useState(null);

  const 刷新 = () =>
    Promise.all([api.切日核证(), api.切日排队(), api.切日批次(), api.切日夹具()]).then(
      ([z, q, b, f]) => {
        set核证(z);
        set队(q);
        set批(b);
        set夹具们(f);
      }
    );

  useEffect(() => {
    刷新().catch((e) => set讯(String(e.message || e)));
    const t = setInterval(() => api.切日核证().then(set核证).catch(() => {}), 1500);
    return () => clearInterval(t);
  }, []);

  const 跑夹具 = (名) =>
    api
      .运行切日夹具(名)
      .then((r) => {
        set果(r);
        set讯(`${名}：${r.全通过 ? "全通过" : "有断言失败"}（口径 ${r.口径}，双成功允许=${r.双成功允许}）`);
      })
      .then(刷新)
      .catch((e) => set讯(String(e.message || e)));

  return (
    <div>
      <h1 className="题">切日互斥</h1>
      <p className="述">
        口径乙·配对租约排队：装载批、抽取批、切点、回放批、快照回滚同票排队，库内租约实体互斥（禁止进程内私有锁）。
        双成功允许但须守恒；租约到期失败闭并回滚未提交写；无租约拒绝写入。
      </p>

      <div className="阶条">
        <div className="阶">
          <div className="序">当前租约</div>
          <b>{核证?.valid ? "持有中" : "空闲"}</b>
          <span>{核证?.持有方 || "—"}</span>
        </div>
        <div className="阶">
          <div className="序">valid</div>
          <b>{String(Boolean(核证?.valid))}</b>
          <span>核证接口</span>
        </div>
        <div className="阶">
          <div className="序">租约截止</div>
          <b>{核证?.租约截止 ? Number(核证.租约截止).toFixed(1) : "—"}</b>
          <span>单调秒</span>
        </div>
        <div className="阶">
          <div className="序">排队等待</div>
          <b>{核证?.排队等待数 ?? "—"}</b>
          <span>FIFO</span>
        </div>
        <div className="阶">
          <div className="序">口径</div>
          <b style={{ fontSize: "0.95rem" }}>{核证?.口径 || "—"}</b>
          <span>双成功允许</span>
        </div>
      </div>

      <div className="行">
        <input value={切点日} onChange={(e) => set切点日(e.target.value)} placeholder="YYYY-MM-DD" />
        <button
          onClick={() =>
            api
              .执行切点(切点日)
              .then((r) => set讯(`切点 ${r.状态}：${r.自营业日} → ${r.至营业日}（租约 ${r.租约号?.slice(0, 10)}…）`))
              .then(刷新)
              .catch((e) => set讯(String(e.message || e)))
          }
        >
          执行切点（持租约）
        </button>
        <button className="次" onClick={() => 刷新().catch((e) => set讯(String(e.message || e)))}>
          刷新
        </button>
      </div>
      {讯 ? <p className="讯">{讯}</p> : null}

      <div className="段">
        <h2 className="段题">切日并行夹具（断言双成功口径与半截回滚）</h2>
        <div className="行">
          {夹具们.map((f) => (
            <button key={f} className="次" onClick={() => 跑夹具(f)}>
              {f.replace(".json", "")}
            </button>
          ))}
        </div>
        {果 ? (
          <div className="提示条">
            <div>
              <b>{果.场景}</b> · 运行 {果.运行令牌} ·{" "}
              <span className={`徽章 ${果.全通过 ? "通" : "败"}`}>
                {果.全通过 ? "全通过" : "失败"}
              </span>
            </div>
            <table style={{ marginTop: "0.5rem" }}>
              <thead>
                <tr><th>断言</th><th>期望</th><th>实测</th><th>结果</th></tr>
              </thead>
              <tbody>
                {果.断言.map((a) => (
                  <tr key={a.断言}>
                    <td>{a.断言}</td>
                    <td>{String(a.期望)}</td>
                    <td>{String(a.实测)}</td>
                    <td>
                      <span className={`徽章 ${a.通过 ? "通" : "败"}`}>
                        {a.通过 ? "通过" : "不符"}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </div>

      <div className="段">
        <h2 className="段题">切日排队（FIFO 实体）</h2>
        {!队.length ? (
          <div className="空态">暂无排队记录</div>
        ) : (
          <table>
            <thead>
              <tr><th>排队号</th><th>持有方</th><th>作业种类</th><th>状态</th><th>租约号</th><th>备注</th></tr>
            </thead>
            <tbody>
              {队.map((r) => (
                <tr key={r.排队号}>
                  <td>{r.排队号}</td>
                  <td>{r.持有方}</td>
                  <td>{r.作业种类}</td>
                  <td>
                    <span className={`徽章 ${r.状态 === "完成" ? "通" : r.状态 === "失败闭" ? "败" : ""}`}>
                      {r.状态}
                    </span>
                  </td>
                  <td>{r.租约号 ? r.租约号.slice(0, 12) + "…" : "—"}</td>
                  <td>{r.备注 || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="段">
        <h2 className="段题">切日批次台账</h2>
        {!批.length ? (
          <div className="空态">尚无切点</div>
        ) : (
          <table>
            <thead>
              <tr><th>编号</th><th>自营业日</th><th>至营业日</th><th>状态</th><th>租约号</th></tr>
            </thead>
            <tbody>
              {批.map((r) => (
                <tr key={r.编号}>
                  <td>{r.编号}</td>
                  <td>{r.自营业日}</td>
                  <td>{r.至营业日}</td>
                  <td>{r.状态}</td>
                  <td>{r.租约号 ? r.租约号.slice(0, 12) + "…" : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
