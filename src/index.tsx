import {
  ButtonItem,
  ConfirmModal,
  DialogButton,
  DropdownItem,
  Field,
  Focusable,
  ModalRoot,
  PanelSection,
  PanelSectionRow,
  ProgressBarWithInfo,
  TextField,
  ToggleField,
  showModal,
  staticClasses,
} from "@decky/ui";
import { addEventListener, callable, definePlugin, removeEventListener, toaster } from "@decky/api";
import { useCallback, useEffect, useRef, useState } from "react";
import { FaCrown } from "react-icons/fa";
import { errText, t } from "./i18n";

// ---------------------------------------------------------------- types

interface Profile {
  id: number;
  group: number;
  name: string;
  type: string;
  address: string;
  latency_ms: number;
  country: string;
  ip_out: string;
  running: boolean;
}

interface Group {
  id: number;
  name: string;
  subscription: boolean;
  archived: boolean;
  profile_count: number;
  updated_at: number;
  route_id: number;
  import_route: boolean;
}

interface Route {
  id: number;
  name: string;
  active: boolean;
  in_use: boolean;
}

interface Status {
  state: "idle" | "connecting" | "running" | "stopping";
  profile: Profile | null;
  last_profile_id: number;
  tun: boolean;
  tun_conflict: string | null;
  route: { id: number; name: string } | null;
  core: boolean;
  inbound: { enabled: boolean; address: string; port: number };
  version: string;
  capabilities: { tun: boolean | null; kill_switch: boolean; headless: boolean };
  restart_needed: boolean;
  degraded: string | null;
}

interface PluginState {
  installed: boolean;
  version: string;
  service: string;
  api: string;
  error: string;
  status: Status | null;
  busy: string;
  settings: { autoconnect: boolean };
  gui: boolean;
}

type ApiResult = { ok: true; status: number; body: any } | { ok: false; code: string; message: string; status: number };
type Result = { ok: boolean; code?: string; message?: string; [k: string]: any };

interface Progress {
  stage: string;
  done?: number;
  total?: number;
}

// ------------------------------------------------------------ backend

const getState = callable<[], PluginState>("get_state");
const apiCall = callable<[string, string, any, any, number], ApiResult>("api");
const install = callable<[boolean], Result>("install");
const checkUpdate = callable<[], Result>("check_update");
const localZip = callable<[], string>("local_zip");
const service = callable<[string], Result>("service");
const setSetting = callable<[string, any], Result>("set_setting");
const getLogs = callable<[number], string>("get_logs");
const readImportFile = callable<[], string>("read_import_file");

class ApiError extends Error {
  constructor(public code: string, message: string) {
    super(message);
  }
}

async function api<T = any>(method: string, path: string, body?: any, timeout = 15, query?: any): Promise<T> {
  const r = await apiCall(method, path, body ?? null, query ?? null, timeout);
  if (!r.ok) throw new ApiError(r.code, r.message);
  return r.body as T;
}

function toastError(e: unknown) {
  if (e instanceof ApiError) toaster.toast({ title: t.title, body: errText(e.code, e.message) });
  else toaster.toast({ title: t.title, body: `${t.error}: ${String(e)}` });
}

function toastResult(r: Result, okText: string) {
  if (r.ok) toaster.toast({ title: t.title, body: okText });
  else toaster.toast({ title: t.title, body: errText(r.code || "error", r.message || "") });
}

// ------------------------------------------------------------- format

function rate(bps: number): string {
  if (bps < 1024) return `${bps} B/s`;
  if (bps < 1024 * 1024) return `${(bps / 1024).toFixed(0)} KB/s`;
  return `${(bps / 1024 / 1024).toFixed(1)} MB/s`;
}

function latency(p: Profile): string {
  if (p.latency_ms > 0) return `${p.latency_ms} ${t.ms}`;
  if (p.latency_ms < 0) return `✗ ${t.failed}`;
  return "";
}

function flag(cc: string): string {
  if (!/^[A-Za-z]{2}$/.test(cc)) return "";
  const base = 0x1f1e6;
  const up = cc.toUpperCase();
  return String.fromCodePoint(base + up.charCodeAt(0) - 65, base + up.charCodeAt(1) - 65) + " ";
}

function hostName(url: string): string {
  try {
    return new URL(url).hostname;
  } catch {
    return "";
  }
}

const LS_GROUP = "decky-throne.group";
const MAX_LISTED = 40;

// A gamepad press on a Field can fire both onActivate and onClick.
function useOnce() {
  const last = useRef({ key: "", at: 0 });
  return (key: string) => {
    const now = Date.now();
    if (last.current.key === key && now - last.current.at < 600) return false;
    last.current = { key, at: now };
    return true;
  };
}

// --------------------------------------------------------------- modals

function AddModal({ closeModal, onAdded }: { closeModal?: () => void; onAdded: (groupId: number) => void }) {
  const [text, setText] = useState("");
  const [name, setName] = useState("");
  const [importRoute, setImportRoute] = useState(true);
  const [busy, setBusy] = useState(false);
  const trimmed = text.trim();
  const isSub = /^https?:\/\/\S+$/i.test(trimmed);

  const paste = async () => {
    let got = "";
    try {
      got = await navigator.clipboard.readText();
    } catch {
      got = "";
    }
    if (!got.trim()) {
      try {
        got = await readImportFile();
      } catch {
        got = "";
      }
    }
    if (got.trim()) setText(got.trim());
    else toaster.toast({ title: t.title, body: t.pasteFailed });
  };

  const submit = async () => {
    if (!trimmed || busy) return;
    setBusy(true);
    try {
      if (isSub) {
        const r = await api<{ group: Group; update: any }>(
          "POST",
          "/v1/groups",
          { name: name.trim() || hostName(trimmed) || t.subscription, url: trimmed, import_route: importRoute, update: true, timeout: 60 },
          75,
        );
        const u = r.update;
        if (u && u.ok === false) toaster.toast({ title: t.title, body: errText("fetch_failed", u.error || "") });
        else toaster.toast({ title: t.title, body: `${t.added}: ${r.group.name} (${r.group.profile_count})` });
        onAdded(r.group.id);
      } else {
        const groups = (await api<{ groups: Group[] }>("GET", "/v1/groups")).groups;
        let g = groups.find((x) => !x.subscription && !x.archived && x.name === t.linksGroup);
        if (!g) g = (await api<{ group: Group }>("POST", "/v1/groups", { name: t.linksGroup })).group;
        const r = await api<{ imported: number; failed: number }>(
          "POST",
          "/v1/import",
          { as: "profiles", text: trimmed, group: g.id, timeout: 60 },
          75,
        );
        toaster.toast({ title: t.title, body: `${t.added}: ${r.imported}${r.failed ? `, ✗ ${r.failed}` : ""}` });
        onAdded(g.id);
      }
      closeModal?.();
    } catch (e) {
      toastError(e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <ModalRoot closeModal={closeModal} onCancel={closeModal}>
      <div className={staticClasses.Title} style={{ marginBottom: 8 }}>
        {t.addTitle}
      </div>
      <div style={{ fontSize: 13, opacity: 0.8, marginBottom: 12 }}>{t.addHint}</div>
      <TextField label={t.addField} value={text} onChange={(e) => setText(e.target.value)} bShowClearAction />
      {isSub && (
        <>
          <TextField label={t.addName} value={name} placeholder={hostName(trimmed)} onChange={(e) => setName(e.target.value)} />
          <ToggleField label={t.addRouting} description={t.addRoutingDesc} checked={importRoute} onChange={setImportRoute} />
        </>
      )}
      <Focusable style={{ display: "flex", gap: 8, marginTop: 16 }} flow-children="horizontal">
        <DialogButton onClick={paste} disabled={busy}>
          {t.paste}
        </DialogButton>
        <DialogButton onClick={submit} disabled={busy || !trimmed}>
          {busy ? t.updating : t.addButton}
        </DialogButton>
        <DialogButton onClick={closeModal}>{t.cancel}</DialogButton>
      </Focusable>
    </ModalRoot>
  );
}

function LogsModal({ closeModal }: { closeModal?: () => void }) {
  const [text, setText] = useState("…");
  useEffect(() => {
    getLogs(200)
      .then((s) => setText(s.split("\n").reverse().join("\n") || "—"))
      .catch((e) => setText(String(e)));
  }, []);
  return (
    <ModalRoot closeModal={closeModal} onCancel={closeModal}>
      <div className={staticClasses.Title} style={{ marginBottom: 8 }}>
        {t.logs}
      </div>
      <Focusable
        noFocusRing
        style={{
          maxHeight: "60vh",
          overflowY: "auto",
          whiteSpace: "pre-wrap",
          wordBreak: "break-all",
          fontFamily: "monospace",
          fontSize: 11,
        }}
      >
        {text}
      </Focusable>
    </ModalRoot>
  );
}

// ---------------------------------------------------------------- panel

function InstallSection({ state, refresh }: { state: PluginState; refresh: () => void }) {
  const [progress, setProgress] = useState<Progress | null>(null);
  const [local, setLocal] = useState("");
  const busy = state.busy === "install" || progress !== null;

  useEffect(() => {
    localZip().then(setLocal).catch(() => {});
    const l = addEventListener<[Progress]>("throne_progress", (p) => setProgress(p.stage === "done" ? null : p));
    return () => {
      removeEventListener("throne_progress", l);
    };
  }, []);

  const run = async (preferLocal: boolean) => {
    setProgress({ stage: "check" });
    const r = await install(preferLocal).catch((e) => ({ ok: false, code: "error", message: String(e) }) as Result);
    setProgress(null);
    toastResult(r, `${t.installed}: ${r.version ?? ""}`);
    refresh();
  };

  const stageText: Record<string, string> = {
    check: t.stageCheck,
    download: t.stageDownload,
    verify: t.stageVerify,
    extract: t.stageExtract,
    start: t.stageStart,
  };
  const pct = progress?.total ? (100 * (progress.done || 0)) / progress.total : undefined;

  return (
    <PanelSection title={t.notInstalled}>
      <PanelSectionRow>
        <div style={{ fontSize: 13, opacity: 0.8 }}>{t.notInstalledHint}</div>
      </PanelSectionRow>
      {busy ? (
        <PanelSectionRow>
          <ProgressBarWithInfo
            indeterminate={pct === undefined}
            nProgress={pct}
            sOperationText={stageText[progress?.stage || "check"] || t.installing}
          />
        </PanelSectionRow>
      ) : (
        <>
          <PanelSectionRow>
            <ButtonItem layout="below" onClick={() => run(false)}>
              {t.install}
            </ButtonItem>
          </PanelSectionRow>
          {local && (
            <PanelSectionRow>
              <ButtonItem layout="below" description={local.split("/").pop()} onClick={() => run(true)}>
                {t.installLocal}
              </ButtonItem>
            </PanelSectionRow>
          )}
        </>
      )}
    </PanelSection>
  );
}

function Content() {
  const [state, setState] = useState<PluginState | null>(null);
  const [groups, setGroups] = useState<Group[]>([]);
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [routes, setRoutes] = useState<Route[]>([]);
  const [groupId, setGroupId] = useState<number>(() => Number(localStorage.getItem(LS_GROUP) ?? -1));
  const [live, setLive] = useState<{ up: number; down: number } | null>(null);
  const [action, setAction] = useState("");
  const [update, setUpdate] = useState<Result | null>(null);
  const once = useOnce();
  const mounted = useRef(true);

  const status = state?.status ?? null;
  const apiOk = state?.api === "ok" && !!status;
  const running = status?.state === "running" || status?.state === "connecting";

  const refresh = useCallback(async () => {
    try {
      const s = await getState();
      if (mounted.current) setState(s);
    } catch {
      /* Decky not ready */
    }
  }, []);

  const loadLists = useCallback(async () => {
    try {
      const [g, r] = await Promise.all([
        api<{ groups: Group[] }>("GET", "/v1/groups"),
        api<{ routes: Route[] }>("GET", "/v1/routes"),
      ]);
      if (!mounted.current) return;
      const visible = g.groups.filter((x) => !x.archived);
      setGroups(visible);
      setRoutes(r.routes);
      setGroupId((cur) => {
        if (visible.some((x) => x.id === cur)) return cur;
        const withProfiles = visible.find((x) => x.profile_count > 0);
        return (withProfiles ?? visible[0])?.id ?? -1;
      });
    } catch {
      /* shown through state.api */
    }
  }, []);

  const loadProfiles = useCallback(async (gid: number) => {
    if (gid < 0) {
      setProfiles([]);
      return;
    }
    try {
      const r = await api<{ profiles: Profile[] }>("GET", "/v1/profiles", null, 15, { group: String(gid) });
      if (mounted.current) setProfiles(r.profiles);
    } catch {
      if (mounted.current) setProfiles([]);
    }
  }, []);

  // state every 2 s while the panel is open
  useEffect(() => {
    mounted.current = true;
    refresh();
    const id = setInterval(refresh, 2000);
    return () => {
      mounted.current = false;
      clearInterval(id);
    };
  }, [refresh]);

  useEffect(() => {
    if (apiOk) loadLists();
  }, [apiOk, loadLists]);

  useEffect(() => {
    if (groupId >= 0) localStorage.setItem(LS_GROUP, String(groupId));
    if (apiOk) loadProfiles(groupId);
  }, [groupId, apiOk, status?.profile?.id, loadProfiles]);

  // live traffic while connected
  useEffect(() => {
    if (status?.state !== "running") {
      setLive(null);
      return;
    }
    const tick = () =>
      api<{ proxy: { up: number; down: number } }>("GET", "/v1/stats/live", null, 5)
        .then((s) => mounted.current && setLive(s.proxy))
        .catch(() => {});
    tick();
    const id = setInterval(tick, 2000);
    return () => clearInterval(id);
  }, [status?.state]);

  const act = async (name: string, fn: () => Promise<void>) => {
    if (action) return;
    setAction(name);
    try {
      await fn();
    } catch (e) {
      toastError(e);
    } finally {
      if (mounted.current) setAction("");
      refresh();
    }
  };

  const connectTo = (id: number) =>
    act("connect", async () => {
      await api("PUT", "/v1/connection", { profile: id, restart: status?.profile?.id === id, timeout: 60 }, 70);
    });

  const toggleVpn = (on: boolean) =>
    act("toggle", async () => {
      if (!on) {
        await api("DELETE", "/v1/connection", { timeout: 30 }, 40);
        return;
      }
      if (status && status.last_profile_id >= 0) {
        await api("PUT", "/v1/connection", { select: "last", timeout: 60 }, 70);
      } else if (profiles.length > 0) {
        await api("PUT", "/v1/connection", { profile: profiles[0].id, timeout: 60 }, 70);
      } else {
        throw new ApiError("no_profiles", "");
      }
    });

  const best = () =>
    act("best", async () => {
      await api("PUT", "/v1/connection", { select: "lowest_latency", group: groupId, max_age: 300, timeout: 120 }, 130);
    });

  const ping = () =>
    act("ping", async () => {
      await api("POST", "/v1/tests", { kind: "latency", group: groupId, wait: true, timeout: 120 }, 130);
      await loadProfiles(groupId);
    });

  const group = groups.find((g) => g.id === groupId);

  const updateSub = () =>
    act("update", async () => {
      const r = await api<{ group: Group; update: any }>("POST", `/v1/groups/${groupId}/update`, { timeout: 60 }, 75);
      const u = r.update;
      if (u?.ok) toaster.toast({ title: group?.name ?? t.title, body: t.updated(u.added, u.updated, u.removed) });
      else if (u) toaster.toast({ title: group?.name ?? t.title, body: errText("fetch_failed", u.error || "") });
      await loadLists();
      await loadProfiles(groupId);
    });

  const deleteGroup = () => {
    if (!group) return;
    showModal(
      <ConfirmModal
        strTitle={t.deleteSub}
        strDescription={t.deleteConfirm(group.name)}
        strOKButtonText={t.delete}
        strCancelButtonText={t.cancel}
        bDestructiveWarning
        onOK={() =>
          act("delete", async () => {
            await api("DELETE", `/v1/groups/${group.id}`, { timeout: 30 }, 40);
            setGroupId(-1);
            await loadLists();
          })
        }
      />,
    );
  };

  const openAdd = () =>
    showModal(
      <AddModal
        onAdded={(gid) => {
          setGroupId(gid);
          loadLists();
          loadProfiles(gid);
        }}
      />,
    );

  const setTun = (on: boolean) =>
    act("tun", async () => {
      await api("PUT", "/v1/modes", { tun: on, timeout: 60 }, 70);
    });

  const setRoute = (id: number) =>
    act("route", async () => {
      await api("PUT", "/v1/routes/active", { id, timeout: 60 }, 70);
      await loadLists();
    });

  const svc = (a: string) =>
    act("service", async () => {
      const r = await service(a);
      if (!r.ok) throw new ApiError(r.code || "error", r.message || "");
    });

  const doCheckUpdate = () =>
    act("check", async () => {
      const r = await checkUpdate();
      if (!r.ok) throw new ApiError(r.code || "error", r.message || "");
      setUpdate(r);
      if (!r.update) toaster.toast({ title: t.title, body: t.upToDate(r.installed) });
    });

  const doUpdate = () =>
    act("install", async () => {
      const r = await install(false);
      toastResult(r, `${t.installed}: ${r.version ?? ""}`);
      setUpdate(null);
    });

  // ------------------------------------------------------------- render

  if (!state) return null;
  if (!state.installed) return <InstallSection state={state} refresh={refresh} />;

  const footer = (
    <PanelSection title={t.settings}>
      {apiOk && status && (
        <PanelSectionRow>
          <ToggleField
            label={t.tun}
            description={status.capabilities.tun === false ? t.tunUnavailable : status.tun ? undefined : t.tunDesc(status.inbound.port)}
            checked={status.tun}
            disabled={!!action || status.capabilities.tun === false}
            onChange={setTun}
          />
        </PanelSectionRow>
      )}
      <PanelSectionRow>
        <ToggleField
          label={t.autoconnect}
          description={t.autoconnectDesc}
          checked={!!state.settings.autoconnect}
          onChange={(v) => setSetting("autoconnect", v).then(refresh)}
        />
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem layout="below" onClick={() => showModal(<LogsModal />)}>
          {t.logs}
        </ButtonItem>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem layout="below" disabled={!!action} onClick={() => svc("restart")}>
          {action === "service" ? "…" : t.restartService}
        </ButtonItem>
      </PanelSectionRow>
      <PanelSectionRow>
        {update?.update ? (
          <ButtonItem layout="below" disabled={!!action} onClick={doUpdate}>
            {action === "install" ? t.installing : t.updateThrone(update.installed, update.latest)}
          </ButtonItem>
        ) : (
          <ButtonItem layout="below" disabled={!!action} onClick={doCheckUpdate}>
            {t.checkUpdate}
          </ButtonItem>
        )}
      </PanelSectionRow>
      <PanelSectionRow>
        <div style={{ fontSize: 11, opacity: 0.6 }}>{t.footer(state.version || "?", state.service)}</div>
      </PanelSectionRow>
    </PanelSection>
  );

  if (state.gui && (!apiOk || !status)) {
    return (
      <>
        <PanelSection title={t.vpn}>
          <PanelSectionRow>
            <Field label={t.guiOpen} description={t.guiOpenDesc} />
          </PanelSectionRow>
        </PanelSection>
        {footer}
      </>
    );
  }

  if (!apiOk || !status) {
    const msg =
      state.service !== "active" && state.service !== "activating"
        ? t.serviceDown
        : state.api === "starting" || state.service === "activating"
          ? t.apiStarting
          : t.apiDown;
    return (
      <>
        <PanelSection title={t.vpn}>
          <PanelSectionRow>
            <Field label={msg} description={state.error ? errText(state.api, state.error) : undefined} />
          </PanelSectionRow>
          <PanelSectionRow>
            <ButtonItem layout="below" disabled={!!action} onClick={() => svc("start")}>
              {action === "service" ? "…" : t.startService}
            </ButtonItem>
          </PanelSectionRow>
        </PanelSection>
        {footer}
      </>
    );
  }

  const stateText =
    status.state === "running"
      ? t.connected
      : status.state === "connecting"
        ? t.connecting
        : status.state === "stopping"
          ? t.stopping
          : t.disconnected;
  const cur = status.profile;
  const curDesc = cur
    ? [flag(cur.country) + cur.name, latency(cur), live ? `${t.down} ${rate(live.down)}  ${t.up} ${rate(live.up)}` : ""]
        .filter(Boolean)
        .join(" · ")
    : undefined;
  const listed = profiles.slice(0, MAX_LISTED);
  const activeRoute = routes.find((r) => r.active);

  return (
    <>
      <PanelSection title={t.vpn}>
        <PanelSectionRow>
          <ToggleField
            label={stateText}
            description={curDesc}
            checked={running}
            disabled={!!action || status.state === "stopping"}
            onChange={toggleVpn}
          />
        </PanelSectionRow>
        {status.degraded && (
          <PanelSectionRow>
            <Field label={t.degraded} description={errText(status.degraded, status.degraded)} />
          </PanelSectionRow>
        )}
        {status.restart_needed && running && (
          <PanelSectionRow>
            <ButtonItem layout="below" description={t.restartNeeded} disabled={!!action} onClick={() => cur && connectTo(cur.id)}>
              {t.reconnect}
            </ButtonItem>
          </PanelSectionRow>
        )}
      </PanelSection>

      <PanelSection title={t.server}>
        {groups.length === 0 ? (
          <PanelSectionRow>
            <Field label={t.noGroups} />
          </PanelSectionRow>
        ) : (
          <>
            {groups.length > 1 && (
              <PanelSectionRow>
                <DropdownItem
                  label={t.subscription}
                  rgOptions={groups.map((g) => ({ data: g.id, label: `${g.name} (${g.profile_count})` }))}
                  selectedOption={groupId}
                  onChange={(o) => setGroupId(o.data)}
                />
              </PanelSectionRow>
            )}
            {profiles.length === 0 && (
              <PanelSectionRow>
                <Field label={t.noProfiles} />
              </PanelSectionRow>
            )}
            {listed.map((p) => (
              <PanelSectionRow key={p.id}>
                <Field
                  focusable
                  highlightOnFocus
                  label={
                    <span style={{ color: p.running ? "#4ade80" : undefined }}>
                      {p.running ? "● " : ""}
                      {flag(p.country)}
                      {p.name}
                    </span>
                  }
                  description={[p.type, latency(p)].filter(Boolean).join(" · ")}
                  onActivate={() => once(`p${p.id}`) && connectTo(p.id)}
                  onClick={() => once(`p${p.id}`) && connectTo(p.id)}
                />
              </PanelSectionRow>
            ))}
            {profiles.length > MAX_LISTED && (
              <PanelSectionRow>
                <div style={{ fontSize: 12, opacity: 0.7 }}>{t.more(profiles.length - MAX_LISTED)}</div>
              </PanelSectionRow>
            )}
            {profiles.length > 1 && (
              <PanelSectionRow>
                <ButtonItem layout="below" description={t.bestDesc} disabled={!!action} onClick={best}>
                  {action === "best" ? t.pinging : t.best}
                </ButtonItem>
              </PanelSectionRow>
            )}
            {profiles.length > 0 && (
              <PanelSectionRow>
                <ButtonItem layout="below" disabled={!!action} onClick={ping}>
                  {action === "ping" ? t.pinging : t.ping}
                </ButtonItem>
              </PanelSectionRow>
            )}
            {group?.subscription && (
              <PanelSectionRow>
                <ButtonItem layout="below" disabled={!!action} onClick={updateSub}>
                  {action === "update" ? t.updating : t.updateSub}
                </ButtonItem>
              </PanelSectionRow>
            )}
          </>
        )}
        <PanelSectionRow>
          <ButtonItem layout="below" disabled={!!action} onClick={openAdd}>
            {t.add}
          </ButtonItem>
        </PanelSectionRow>
        {group && groups.length > 1 && (
          <PanelSectionRow>
            <ButtonItem layout="below" disabled={!!action} onClick={deleteGroup}>
              {t.deleteSub}
            </ButtonItem>
          </PanelSectionRow>
        )}
      </PanelSection>

      {routes.length > 1 && (
        <PanelSection title={t.routing}>
          <PanelSectionRow>
            <DropdownItem
              label={t.routing}
              description={group && group.route_id >= 0 ? t.groupOwnRoute : undefined}
              rgOptions={routes.map((r) => ({ data: r.id, label: r.name }))}
              selectedOption={activeRoute?.id}
              disabled={!!action}
              onChange={(o) => setRoute(o.data)}
            />
          </PanelSectionRow>
        </PanelSection>
      )}

      {footer}
    </>
  );
}

export default definePlugin(() => ({
  name: "Throne VPN",
  titleView: <div className={staticClasses.Title}>{t.title}</div>,
  content: <Content />,
  icon: <FaCrown />,
}));
