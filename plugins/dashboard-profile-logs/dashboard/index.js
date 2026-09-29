(() => {
  const sdk = window.__HERMES_PLUGIN_SDK__;
  const { React, hooks, fetchJSON } = sdk;
  const h = React.createElement;
  const { useState, useEffect, useRef } = hooks;

  function Logs() {
    const { t } = sdk.useI18n();
    const [file, setFile] = useState("agent");
    const [level, setLevel] = useState("ALL");
    const [component, setComponent] = useState("all");
    const [limit, setLimit] = useState("100");
    const [automatic, setAutomatic] = useState(false);
    const [revision, refresh] = useState(0);
    const [result, setResult] = useState({ lines: [], profile: "" });
    const [error, setError] = useState("");
    const [loading, setLoading] = useState(true);
    const scroll = useRef(null);

    useEffect(() => {
      let disposed = false;
      const controller = new AbortController();
      setLoading(true);
      setError("");
      setResult({ lines: [], profile: "" });
      // The native ProfileProvider synchronizes the URL in its parent effect.
      // Read after that commit; its ProfileKeyedRoutes remounts this page on switch.
      Promise.resolve().then(() => {
        if (disposed) return;
        const profile = new URLSearchParams(window.location.search).get("profile") || "current";
        const query = new URLSearchParams({ profile, file, lines: limit, level, component });
        return fetchJSON(`/api/plugins/dashboard-profile-logs/logs?${query}`, {
          signal: controller.signal,
        });
      }).then((response) => {
        if (!disposed && response) setResult(response);
      }).catch((failure) => {
        if (!disposed) setError(String(failure));
      }).finally(() => {
        if (!disposed) setLoading(false);
      });
      return () => { disposed = true; controller.abort(); };
    }, [file, level, component, limit, revision]);

    useEffect(() => {
      if (!automatic) return;
      const timer = setInterval(() => refresh((n) => n + 1), 5000);
      return () => clearInterval(timer);
    }, [automatic]);
    useEffect(() => {
      if (scroll.current) scroll.current.scrollTop = scroll.current.scrollHeight;
    }, [result]);

    const select = (label, value, change, options) => h("label", {
      className: "flex items-center gap-2 text-sm",
    }, label, h("select", {
      "aria-label": label, value, onChange: (event) => change(event.target.value),
      className: "rounded border bg-background px-2 py-1 text-foreground",
    }, options.map((option) => h("option", { key: option, value: option }, option))));

    return h("section", { className: "flex flex-col gap-4 min-w-0", "aria-label": t.logs.title },
      h("div", { className: "flex items-center justify-between gap-3 flex-wrap" },
        h("h2", { className: "text-lg font-semibold" }, t.logs.title),
        h("span", { className: "text-sm text-muted-foreground" }, result.profile),
        h(sdk.components.Button, { onClick: () => refresh((n) => n + 1), disabled: loading }, t.common.refresh)),
      h("div", { className: "flex gap-4 flex-wrap", role: "toolbar", "aria-label": t.logs.title },
        select(t.logs.file, file, setFile, ["agent", "errors", "gateway"]),
        select(t.logs.level, level, setLevel, ["ALL", "DEBUG", "INFO", "WARNING", "ERROR"]),
        select(t.logs.component, component, setComponent, ["all", "gateway", "agent", "tools", "cli", "cron"]),
        select(t.logs.lines, limit, setLimit, ["50", "100", "200", "500"]),
        h("label", { className: "flex items-center gap-2 text-sm" },
          h("input", { type: "checkbox", checked: automatic, onChange: (event) => setAutomatic(event.target.checked) }),
          t.logs.autoRefresh)),
      error && h("p", { role: "alert", className: "text-destructive" }, error),
      h("div", { className: "rounded border overflow-hidden" },
        h("div", { className: "px-4 py-2 border-b font-medium" }, `${file}.log`),
        h("pre", {
          ref: scroll, "aria-busy": loading,
          className: "p-4 font-mono text-xs whitespace-pre-wrap break-words min-h-[400px] max-h-[65vh] overflow-auto",
        }, result.lines.length ? result.lines.join("") : (loading ? "…" : t.logs.noLogLines))));
  }

  window.__HERMES_PLUGINS__.register("dashboard-profile-logs", Logs);
})();
