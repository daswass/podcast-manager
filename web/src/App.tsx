import { useEffect, useRef, useState, type CSSProperties, type RefObject } from "react";
import {
  ApiError,
  createFolder,
  createShow,
  deleteFolder,
  deleteHeadphoneFile,
  deleteShow,
  archiveAudioUrl,
  getArchive,
  getHeadphones,
  getLibrary,
  patchFolder,
  patchShow,
  cutShowAds,
  prepareShow,
  resolveFeed,
  searchShows,
  type Folder,
  type HeadphoneFile,
  type ShowPatch,
  type ArchiveEpisode,
  type HeadphoneVolume,
  type JobActivity,
  type Library,
  type SearchHit,
  type Show,
  type ShowStatus,
} from "./api";

type View = "library" | "player" | "headphones";

type Playback = {
  current: ArchiveEpisode | null;
  playing: boolean;
  progress: number;
  duration: number;
  play: (episode: ArchiveEpisode) => void;
  toggle: () => void;
  seek: (seconds: number) => void;
  close: () => void;
};

function isLocalArchivePath(path: string): boolean {
  return path.startsWith("sliced/") || path.startsWith("trimmed/");
}

function usePlayback(): Playback & { audioRef: RefObject<HTMLAudioElement | null> } {
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const [current, setCurrent] = useState<ArchiveEpisode | null>(null);
  const [playing, setPlaying] = useState(false);
  const [progress, setProgress] = useState(0);
  const [duration, setDuration] = useState(0);

  useEffect(() => {
    const audio = audioRef.current;
    if (!audio) return;
    const onTime = () => setProgress(audio.currentTime || 0);
    const onMeta = () => setDuration(Number.isFinite(audio.duration) ? audio.duration : 0);
    const onPlay = () => setPlaying(true);
    const onPause = () => setPlaying(false);
    audio.addEventListener("timeupdate", onTime);
    audio.addEventListener("loadedmetadata", onMeta);
    audio.addEventListener("durationchange", onMeta);
    audio.addEventListener("play", onPlay);
    audio.addEventListener("pause", onPause);
    audio.addEventListener("ended", onPause);
    return () => {
      audio.removeEventListener("timeupdate", onTime);
      audio.removeEventListener("loadedmetadata", onMeta);
      audio.removeEventListener("durationchange", onMeta);
      audio.removeEventListener("play", onPlay);
      audio.removeEventListener("pause", onPause);
      audio.removeEventListener("ended", onPause);
    };
  }, []);

  function play(episode: ArchiveEpisode) {
    const audio = audioRef.current;
    if (!audio || !isLocalArchivePath(episode.path)) return;
    if (current?.path === episode.path) {
      if (audio.paused) void audio.play().catch(() => undefined);
      else audio.pause();
      return;
    }
    setCurrent(episode);
    setProgress(0);
    setDuration(0);
    audio.src = archiveAudioUrl(episode.path);
    void audio.play().catch(() => undefined);
  }

  function toggle() {
    const audio = audioRef.current;
    if (!audio || !audio.src) return;
    if (audio.paused) void audio.play().catch(() => undefined);
    else audio.pause();
  }

  function seek(seconds: number) {
    const audio = audioRef.current;
    if (!audio) return;
    audio.currentTime = seconds;
    setProgress(seconds);
  }

  function close() {
    const audio = audioRef.current;
    if (audio) {
      audio.pause();
      audio.removeAttribute("src");
      audio.load();
    }
    setCurrent(null);
    setPlaying(false);
    setProgress(0);
    setDuration(0);
  }

  return { audioRef, current, playing, progress, duration, play, toggle, seek, close };
}

type DropTarget = { name: string; place: "before" | "after" };
type Save = (key: string, run: (version: string) => Promise<Library>) => Promise<string | null>;

export function App() {
  const [view, setView] = useState<View>("library");
  const [library, setLibrary] = useState<Library | null>(null);
  const [loadError, setLoadError] = useState("");
  const [notice, setNotice] = useState("");
  const [resetToken, setResetToken] = useState(0);
  const [activeFolder, setActiveFolder] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [archive, setArchive] = useState<ArchiveEpisode[] | null>(null);
  const [archiveError, setArchiveError] = useState("");
  const playback = usePlayback();
  const versionRef = useRef("");
  const generation = useRef(0);
  const hasLibrary = useRef(false);
  const chains = useRef(new Map<string, Promise<string | null>>());

  function accept(next: Library) {
    generation.current += 1;
    versionRef.current = next.version;
    hasLibrary.current = true;
    setLibrary(next);
    setLoadError("");
  }

  async function reloadFromDisk() {
    const next = await getLibrary();
    accept(next);
    setResetToken((value) => value + 1);
    setNotice("Reloaded. The file on disk has the latest values.");
  }

  function enqueue(key: string, run: (version: string) => Promise<Library>) {
    const previous = chains.current.get(key) ?? Promise.resolve();
    const next = previous.catch(() => undefined).then(async () => {
      try {
        accept(await run(versionRef.current));
        return null;
      } catch (error) {
        const message = error instanceof Error ? error.message : "Save failed";
        if (error instanceof ApiError && (error.status === 409 || error.status === 400)) {
          await reloadFromDisk();
          if (error.status === 400) setNotice(message);
          return message;
        }
        setNotice(message);
        return message;
      }
    });
    chains.current.set(key, next);
    return next;
  }

  useEffect(() => {
    let cancelled = false;
    let running = false;
    let again = false;

    async function load() {
      if (cancelled) return;
      if (running) {
        again = true;
        return;
      }
      running = true;
      again = false;
      const seen = generation.current;
      try {
        const next = await getLibrary();
        if (cancelled || seen !== generation.current) return;
        versionRef.current = next.version;
        hasLibrary.current = true;
        setLibrary(next);
        setLoadError("");
      } catch (error) {
        if (cancelled || seen !== generation.current) return;
        const message = error instanceof Error ? error.message : "Could not load the library";
        if (hasLibrary.current) setNotice(message);
        else setLoadError(message);
      } finally {
        running = false;
        if (!cancelled && (again || seen !== generation.current)) {
          again = false;
          void load();
        }
      }
    }

    void load();
    const id = window.setInterval(() => void load(), 1000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, []);

  async function reloadArchive() {
    try {
      setArchive(await getArchive());
      setArchiveError("");
    } catch (error) {
      setArchiveError(error instanceof Error ? error.message : "Could not read the local episodes");
    }
  }

  useEffect(() => {
    void reloadArchive();
  }, []);

  return (
    <div className={`shell${playback.current ? " with-player" : ""}`}>
      <audio ref={playback.audioRef} className="player-audio" preload="metadata" />
      <header className="topbar">
        <div>
          <p className="brand">Podcast Manager</p>
          <p className="tagline">Shows, folders, and the headphone drive</p>
        </div>
        <div className="top-status">
          <ActivityBadges job={library?.job ?? null} />
          <nav className="nav">
          <button type="button" className={view === "library" ? "active" : ""} onClick={() => setView("library")}>
            Library
          </button>
          <button type="button" className={view === "player" ? "active" : ""} onClick={() => setView("player")}>
            Player
          </button>
          <button
            type="button"
            className={view === "headphones" ? "active" : ""}
            onClick={() => setView("headphones")}
          >
            Headphones
          </button>
          </nav>
        </div>
      </header>
      {notice ? (
        <div className="notice" role="status">
          <p>{notice}</p>
          <button type="button" className="dismiss" onClick={() => setNotice("")}>Dismiss</button>
        </div>
      ) : null}
      {loadError && !library ? <p className="banner">{loadError}</p> : null}
      {library ? (
        <Main
          view={view}
          library={library}
          resetToken={resetToken}
          activeFolder={activeFolder}
          adding={adding}
          enqueue={enqueue}
          onActivate={setActiveFolder}
          onAdding={setAdding}
          onNotice={setNotice}
          onPrepare={async (feedId) => {
            try {
              accept(await prepareShow(feedId));
            } catch (error) {
              setNotice(error instanceof Error ? error.message : "Prepare did not start");
              if (error instanceof ApiError && error.status === 409) await reloadFromDisk();
            }
          }}
          onCutAds={async (feedId) => {
            try {
              accept(await cutShowAds(feedId));
            } catch (error) {
              setNotice(error instanceof Error ? error.message : "Ad removal did not start");
              if (error instanceof ApiError && error.status === 409) await reloadFromDisk();
            }
          }}
          archive={archive}
          archiveError={archiveError}
          onOpenEpisodes={() => void reloadArchive()}
          playback={playback}
        />
      ) : null}
      <PlayerDock playback={playback} shows={library?.shows ?? []} />
    </div>
  );
}

function ActivityBadge(props: { kind: "downloading" | "copying" | "running"; label: string; detail: string }) {
  return (
    <span className={`activity ${props.kind}`} title={props.detail}>
      {props.label}
    </span>
  );
}

function ActivityBadges(props: { job: JobActivity }) {
  switch (props.job) {
    case null:
      return null;
    case "downloading":
      return <ActivityBadge kind="downloading" label="Downloading" detail="A prepare is downloading and processing shows." />;
    case "copying":
      return <ActivityBadge kind="copying" label="Copying" detail="Files are being copied to the headphones." />;
    case "both":
      return (
        <>
          <ActivityBadge kind="downloading" label="Downloading" detail="A prepare is downloading and processing shows." />
          <ActivityBadge kind="copying" label="Copying" detail="Files are being copied to the headphones." />
        </>
      );
    case "running":
      return <ActivityBadge kind="running" label="Running" detail="A download or headphone copy is running." />;
    default: {
      const unexpected: never = props.job;
      return unexpected;
    }
  }
}

function Main(props: {
  view: View;
  library: Library;
  resetToken: number;
  activeFolder: string | null;
  adding: boolean;
  enqueue: Save;
  onActivate: (folder: string) => void;
  onAdding: (open: boolean) => void;
  onNotice: (message: string) => void;
  onPrepare: (feedId: number) => Promise<void>;
  onCutAds: (feedId: number) => Promise<void>;
  archive: ArchiveEpisode[] | null;
  archiveError: string;
  onOpenEpisodes: () => void;
  playback: Playback;
}) {
  switch (props.view) {
    case "library":
      return (
        <LibraryView
          library={props.library}
          resetToken={props.resetToken}
          adding={props.adding}
          activeFolder={props.activeFolder}
          enqueue={props.enqueue}
          onActivate={props.onActivate}
          onAdding={props.onAdding}
          onNotice={props.onNotice}
          onPrepare={props.onPrepare}
          onCutAds={props.onCutAds}
          archive={props.archive}
          archiveError={props.archiveError}
          onOpenEpisodes={props.onOpenEpisodes}
          playback={props.playback}
        />
      );
    case "player":
      return (
        <PlayerView
          shows={props.library.shows}
          episodes={props.archive}
          error={props.archiveError}
          playback={props.playback}
        />
      );
    case "headphones":
      return <HeadphonesView shows={props.library.shows} onNotice={props.onNotice} />;
    default: {
      const unexpected: never = props.view;
      return unexpected;
    }
  }
}

function byNewestPosted(shows: Show[]): Show[] {
  return [...shows].sort((left, right) => (right.posted ?? -1) - (left.posted ?? -1));
}

function sameNames(left: string[], right: string[]): boolean {
  return left.length === right.length && left.every((name) => right.includes(name));
}

function placeFolder(names: string[], source: string, target: string, place: "before" | "after"): string[] {
  const next = names.filter((name) => name !== source);
  let index = next.indexOf(target);
  if (index < 0) return names;
  if (place === "after") index += 1;
  next.splice(index, 0, source);
  return next;
}

function dropAt(clientY: number): DropTarget | null {
  const sections = [...document.querySelectorAll<HTMLElement>(".folder[data-folder]")];
  let best: { name: string; place: "before" | "after"; distance: number } | null = null;
  for (const section of sections) {
    const name = section.dataset.folder;
    if (!name) continue;
    const box = section.getBoundingClientRect();
    const distance = clientY < box.top ? box.top - clientY : clientY > box.bottom ? clientY - box.bottom : 0;
    if (distance > 24) continue;
    if (best && distance > best.distance) continue;
    const place = clientY < box.top + box.height / 2 ? "before" : "after";
    best = { name, place, distance };
  }
  return best ? { name: best.name, place: best.place } : null;
}

function LibraryView(props: {
  library: Library;
  resetToken: number;
  adding: boolean;
  activeFolder: string | null;
  enqueue: Save;
  onActivate: (folder: string) => void;
  onAdding: (open: boolean) => void;
  onNotice: (message: string) => void;
  onPrepare: (feedId: number) => Promise<void>;
  onCutAds: (feedId: number) => Promise<void>;
  archive: ArchiveEpisode[] | null;
  archiveError: string;
  onOpenEpisodes: () => void;
  playback: Playback;
}) {
  const [folderName, setFolderName] = useState("");
  const [preview, setPreview] = useState<string[] | null>(null);
  const [dragging, setDragging] = useState<string | null>(null);
  const [drop, setDrop] = useState<DropTarget | null>(null);
  const orderRef = useRef<string[] | null>(null);
  const { library } = props;
  const savedOrder = library.folders.map((folder) => folder.name);
  const visibleOrder = preview && sameNames(preview, savedOrder) ? preview : savedOrder;
  const folders = visibleOrder
    .map((name) => library.folders.find((folder) => folder.name === name))
    .filter((folder): folder is Folder => folder !== undefined);
  const initialFolder = folders.some((folder) => folder.name === props.activeFolder)
    ? props.activeFolder
    : folders[0]?.name ?? null;

  useEffect(() => {
    if (!orderRef.current) return;
    if (sameNames(orderRef.current, savedOrder) && orderRef.current.every((name, index) => name === savedOrder[index])) {
      orderRef.current = null;
      setPreview(null);
    }
  }, [library, savedOrder]);

  useEffect(() => {
    if (!dragging) return;
    const previous = document.body.style.cursor;
    document.body.style.cursor = "grabbing";
    return () => {
      document.body.style.cursor = previous;
    };
  }, [dragging]);

  function currentNames() {
    return orderRef.current ?? library.folders.map((folder) => folder.name);
  }

  function commitOrder(name: string, names: string[]) {
    orderRef.current = names;
    setPreview(names);
    void props.enqueue(`folder-${name}`, (version) =>
      patchFolder(name, { version, index: names.indexOf(name) }),
    ).then((message) => {
      if (message) {
        orderRef.current = null;
        setPreview(null);
      }
    });
  }

  function moveBy(name: string, delta: number) {
    const names = currentNames().slice();
    const index = names.indexOf(name);
    const nextIndex = index + delta;
    if (index < 0 || nextIndex < 0 || nextIndex >= names.length) return;
    names.splice(index, 1);
    names.splice(nextIndex, 0, name);
    commitOrder(name, names);
  }

  function beginReorder(name: string, pointerId: number, handle: HTMLButtonElement) {
    const names = currentNames();
    setDragging(name);
    const move = (event: PointerEvent) => {
      if (event.pointerId !== pointerId) return;
      setDrop(dropAt(event.clientY));
    };
    const finish = (event: PointerEvent) => {
      if (event.pointerId !== pointerId) return;
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", finish);
      handle.removeEventListener("pointercancel", finish);
      const hit = dropAt(event.clientY);
      setDragging(null);
      setDrop(null);
      if (!hit || hit.name === name) return;
      const next = placeFolder(names, name, hit.name, hit.place);
      if (next.join("\0") === names.join("\0")) return;
      commitOrder(name, next);
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", finish);
    handle.addEventListener("pointercancel", finish);
  }

  return (
    <main className="page">
      <div className="toolbar">
        <form
          className="add-folder"
          onSubmit={(event) => {
            event.preventDefault();
            const name = folderName.trim();
            if (!name) return;
            void props.enqueue("new-folder", (version) => createFolder(version, name));
            setFolderName("");
          }}
        >
          <input
            value={folderName}
            placeholder="New folder"
            aria-label="New folder"
            onChange={(event) => setFolderName(event.target.value)}
          />
          <button type="submit">Add folder</button>
        </form>
        <button type="button" className="primary" onClick={() => props.onAdding(true)} disabled={library.folders.length === 0}>
          Add show
        </button>
      </div>
      {folders.length === 0 ? <p className="empty">Add a folder, then add a show.</p> : null}
      {folders.map((folder) => (
        <FolderSection
          key={folder.name}
          queueKey={`folder-${folder.name}`}
          folder={folder}
          shows={library.shows.filter((show) => show.folder === folder.name)}
          folders={library.folders}
          dragging={dragging === folder.name}
          dropPlace={drop?.name === folder.name ? drop.place : null}
          onReorderStart={beginReorder}
          onMove={moveBy}
          resetToken={props.resetToken}
          prepareBusy={library.prepare_busy}
          prepareReason={library.prepare_reason}
          headphoneVolume={library.headphone_volume}
          enqueue={props.enqueue}
          onActivate={() => props.onActivate(folder.name)}
          onNotice={props.onNotice}
          onPrepare={props.onPrepare}
          onCutAds={props.onCutAds}
          archive={props.archive}
          archiveError={props.archiveError}
          onOpenEpisodes={props.onOpenEpisodes}
          playback={props.playback}
        />
      ))}
      {props.adding && initialFolder ? (
        <AddShowDialog
          folders={folders.map((folder) => folder.name)}
          initialFolder={initialFolder}
          enqueue={props.enqueue}
          onClose={() => props.onAdding(false)}
        />
      ) : null}
    </main>
  );
}

function FolderSection(props: {
  queueKey: string;
  folder: { name: string; cut_ads: boolean };
  shows: Show[];
  folders: { name: string; cut_ads: boolean }[];
  dragging: boolean;
  dropPlace: "before" | "after" | null;
  onReorderStart: (name: string, pointerId: number, handle: HTMLButtonElement) => void;
  onMove: (name: string, delta: number) => void;
  resetToken: number;
  prepareBusy: boolean;
  prepareReason: string | null;
  headphoneVolume: string | null;
  enqueue: Save;
  onActivate: () => void;
  onNotice: (message: string) => void;
  onPrepare: (feedId: number) => Promise<void>;
  onCutAds: (feedId: number) => Promise<void>;
  archive: ArchiveEpisode[] | null;
  archiveError: string;
  onOpenEpisodes: () => void;
  playback: Playback;
}) {
  const [name, setName] = useState(props.folder.name);
  const [cutAds, setCutAds] = useState(props.folder.cut_ads);
  const [openFeed, setOpenFeed] = useState<number | null>(null);
  const focused = useRef(false);
  const seen = useRef(props.resetToken);
  const serverName = useRef(props.folder.name);
  const nameRef = useRef(props.folder.name);
  const timer = useRef<number | null>(null);

  useEffect(() => {
    const staleReload = props.resetToken !== seen.current;
    if (focused.current && !staleReload) return;
    seen.current = props.resetToken;
    setName(props.folder.name);
    nameRef.current = props.folder.name;
    serverName.current = props.folder.name;
  }, [props.folder.name, props.resetToken]);

  useEffect(() => {
    if (focused.current) return;
    setCutAds(props.folder.cut_ads);
  }, [props.folder.cut_ads]);

  useEffect(() => {
    return () => {
      if (timer.current !== null) window.clearTimeout(timer.current);
    };
  }, []);

  function commitName(value: string) {
    const nextName = value.trim();
    if (!nextName || nextName === serverName.current) return;
    void props.enqueue(props.queueKey, async (version) => {
      const library = await patchFolder(serverName.current, { version, name: nextName });
      serverName.current = nextName;
      return library;
    });
  }

  return (
    <section
      className={["folder", props.dragging ? "dragging" : "", props.dropPlace ? `drop-${props.dropPlace}` : ""]
        .filter(Boolean)
        .join(" ")}
      data-folder={props.folder.name}
      onClick={props.onActivate}
    >
      <div className="folder-head">
        <button
          type="button"
          className="drag-handle"
          aria-label={`Reorder ${props.folder.name}`}
          title="Drag to reorder"
          onPointerDown={(event) => {
            if (event.button !== 0) return;
            event.preventDefault();
            event.currentTarget.setPointerCapture(event.pointerId);
            props.onReorderStart(props.folder.name, event.pointerId, event.currentTarget);
          }}
          onKeyDown={(event) => {
            if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
            event.preventDefault();
            props.onMove(props.folder.name, event.key === "ArrowUp" ? -1 : 1);
          }}
        />
        <input
          className="folder-name"
          aria-label={`${props.folder.name} folder name`}
          value={name}
          onFocus={() => {
            focused.current = true;
          }}
          onBlur={() => {
            focused.current = false;
            commitName(nameRef.current);
          }}
          onChange={(event) => {
            setName(event.target.value);
            nameRef.current = event.target.value;
            if (timer.current !== null) window.clearTimeout(timer.current);
            timer.current = window.setTimeout(() => commitName(nameRef.current), 300);
          }}
        />
        <CutAdsBadge
          on={cutAds}
          title={cutAds ? "This folder cuts ads. Click to keep them." : "This folder keeps ads. Click to cut them."}
          onToggle={() => {
            const next = !cutAds;
            setCutAds(next);
            void props.enqueue(props.queueKey, (version) =>
              patchFolder(serverName.current, { version, cut_ads: next }),
            );
          }}
        />
        <button
          type="button"
          className="quiet danger-text"
          disabled={props.shows.length > 0}
          title={props.shows.length > 0 ? "Move or remove the shows in this folder first" : "Delete folder"}
          onClick={() => {
            if (!window.confirm(`Delete the ${serverName.current} folder?`)) return;
            void props.enqueue(props.queueKey, (version) => deleteFolder(serverName.current, version));
          }}
        >
          Delete
        </button>
      </div>
      {props.shows.length === 0 ? <p className="empty-row">No shows in this folder.</p> : null}
      {byNewestPosted(props.shows).map((show) => (
        <ShowRow
          key={show.feed_id}
          show={show}
          folders={props.folders}
          open={openFeed === show.feed_id}
          resetToken={props.resetToken}
          prepareBusy={props.prepareBusy}
          prepareReason={props.prepareReason}
          headphoneVolume={props.headphoneVolume}
          enqueue={props.enqueue}
          onToggle={() => {
            props.onActivate();
            setOpenFeed((current) => {
              const next = current === show.feed_id ? null : show.feed_id;
              if (next !== null) props.onOpenEpisodes();
              return next;
            });
          }}
          onNotice={props.onNotice}
          onPrepare={props.onPrepare}
          onCutAds={props.onCutAds}
          episodes={props.archive?.filter((episode) => episode.show === show.name) ?? []}
          archiveReady={props.archive !== null}
          archiveError={props.archiveError}
          playback={props.playback}
        />
      ))}
    </section>
  );
}

function ShowRow(props: {
  show: Show;
  folders: { name: string; cut_ads: boolean }[];
  open: boolean;
  resetToken: number;
  prepareBusy: boolean;
  prepareReason: string | null;
  headphoneVolume: string | null;
  enqueue: Save;
  onToggle: () => void;
  onNotice: (message: string) => void;
  onPrepare: (feedId: number) => Promise<void>;
  onCutAds: (feedId: number) => Promise<void>;
  episodes: ArchiveEpisode[];
  archiveReady: boolean;
  archiveError: string;
  playback: Playback;
}) {
  const preparing = props.show.status === "preparing";
  const reason = preparing ? "Preparing this show" : props.prepareReason;
  const [cutsAds, setCutsAds] = useState(props.show.effective_cut_ads);
  useEffect(() => {
    setCutsAds(props.show.effective_cut_ads);
  }, [props.show.effective_cut_ads]);
  return (
    <article className="show" data-feed={props.show.feed_id}>
      <div className="show-main">
        <div className="show-identity">
          {props.show.image ? <ShowArt src={props.show.image} /> : null}
          <div className="show-title">
            <button type="button" className="show-name" onClick={props.onToggle}>
              {props.show.name}
            </button>
            {props.show.url ? (
              <a className="show-url" href={props.show.url} target="_blank" rel="noreferrer" title={props.show.url}>
                {displayUrl(props.show.url)}
              </a>
            ) : null}
          </div>
        </div>
        {props.headphoneVolume && props.show.on_headphones !== null ? (
          <span
            className={`status headphones ${props.show.on_headphones ? "on-headphones" : "off-headphones"}`}
            title={
              props.show.on_headphones
                ? `The latest episode is on ${props.headphoneVolume}`
                : `The latest episode is not on ${props.headphoneVolume}`
            }
            aria-label={
              props.show.on_headphones
                ? `The latest episode is on ${props.headphoneVolume}`
                : `The latest episode is not on ${props.headphoneVolume}`
            }
          >
            <HeadphonesMark />
          </span>
        ) : null}
        {props.show.status === "trimmed" ? (
          <button
            type="button"
            className="status trimmed"
            disabled={props.prepareBusy}
            aria-label="Remove ads from this episode and copy it to the headphones"
            title={
              props.prepareBusy
                ? reason ?? "Prepare is already running"
                : "Remove ads from this episode and copy it to the headphones"
            }
            onClick={() => void props.onCutAds(props.show.feed_id)}
          >
            Ads
          </button>
        ) : (
          <span className={`status ${props.show.status}`} title={statusDetail(props.show.status)}>
            {statusLabel(props.show.status)}
          </span>
        )}
        <button
          type="button"
          className="prepare"
          disabled={props.prepareBusy}
          title={props.prepareBusy ? reason ?? "Prepare is already running" : "Prepare this show and copy it to connected headphones"}
          onClick={() => void props.onPrepare(props.show.feed_id)}
        >
          {preparing ? "Preparing" : "Prepare"}
        </button>
        <button
          type="button"
          className="trash"
          aria-label={`Remove ${props.show.name}`}
          title={`Remove ${props.show.name}`}
          onClick={() => {
            if (!window.confirm(`Remove ${props.show.name} from the library? Audio files stay.`)) return;
            void props.enqueue(`show-${props.show.feed_id}`, (version) => deleteShow(props.show.feed_id, version));
          }}
        >
          <TrashMark />
        </button>
      </div>
      <p className="meta show-options">
        {showOptions(props.show).map((option) => (
          <span key={option}>{option}</span>
        ))}
        <span>
          <CutAdsBadge
            on={cutsAds}
            title={showCutAdsTitle(props.show, cutsAds)}
            onToggle={() => {
              const next = !cutsAds;
              setCutsAds(next);
              void props.enqueue(`show-${props.show.feed_id}`, (version) =>
                patchShow(props.show.feed_id, { version, cut_ads: next }),
              );
            }}
          />
        </span>
      </p>
      {props.show.error ? <p className="row-error">{props.show.error}</p> : null}
      {props.open ? (
        <LocalEpisodes
          episodes={props.episodes}
          ready={props.archiveReady}
          error={props.archiveError}
          playback={props.playback}
        />
      ) : null}
      {props.open ? (
        <ShowEditor
          show={props.show}
          folders={props.folders}
          resetToken={props.resetToken}
          enqueue={props.enqueue}
        />
      ) : null}
    </article>
  );
}

type Draft = {
  folder: string;
  skipFirst: string;
  skipLast: string;
  speed: string;
  count: string;
  cutAds: boolean | null;
};

function ShowArt(props: { src: string }) {
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    setFailed(false);
  }, [props.src]);
  if (failed) return null;
  return <img className="show-art" src={props.src} alt="" onError={() => setFailed(true)} />;
}

function displayUrl(url: string): string {
  try {
    const parsed = new URL(url);
    const path = parsed.pathname === "/" ? "" : parsed.pathname.replace(/\/$/, "");
    return `${parsed.host.replace(/^www\./, "")}${path}`;
  } catch {
    return url;
  }
}

function ShowEditor(props: {
  show: Show;
  folders: { name: string; cut_ads: boolean }[];
  resetToken: number;
  enqueue: Save;
}) {
  const [draft, setDraft] = useState<Draft>(() => draftFrom(props.show));
  const focused = useRef(false);
  const seen = useRef(props.resetToken);
  const timers = useRef(new Map<string, number>());

  useEffect(() => {
    const staleReload = props.resetToken !== seen.current;
    if (focused.current && !staleReload) return;
    seen.current = props.resetToken;
    setDraft(draftFrom(props.show));
  }, [
    props.resetToken,
    props.show.folder,
    props.show.skip_first,
    props.show.skip_last,
    props.show.playback_speed,
    props.show.num_episodes,
    props.show.cut_ads,
  ]);

  useEffect(() => {
    const pending = timers.current;
    return () => {
      for (const id of pending.values()) window.clearTimeout(id);
    };
  }, []);

  function save(fields: ShowPatch, delay: number, key: string) {
    const existing = timers.current.get(key);
    if (existing !== undefined) window.clearTimeout(existing);
    const send = () => {
      timers.current.delete(key);
      void props.enqueue(`show-${props.show.feed_id}`, (version) =>
        patchShow(props.show.feed_id, { ...fields, version }),
      );
    };
    if (delay === 0) {
      send();
      return;
    }
    timers.current.set(key, window.setTimeout(send, delay));
  }

  return (
    <div
      className="editor"
      onFocus={() => {
        focused.current = true;
      }}
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget as Node | null)) focused.current = false;
      }}
    >
      <label>
        Folder
        <select
          value={draft.folder}
          onChange={(event) => {
            const folder = event.target.value;
            setDraft({ ...draft, folder });
            save({ folder }, 0, "folder");
          }}
        >
          {props.folders.map((folder) => (
            <option key={folder.name} value={folder.name}>{folder.name}</option>
          ))}
        </select>
      </label>
      <label>
        Start trim
        <input
          type="number"
          min={0}
          value={draft.skipFirst}
          onChange={(event) => {
            const skipFirst = event.target.value;
            setDraft({ ...draft, skipFirst });
            const parsed = parseWhole(skipFirst);
            if (parsed !== null) save({ skip_first: parsed }, 300, "skip_first");
          }}
        />
      </label>
      <label>
        End trim
        <input
          type="number"
          min={0}
          value={draft.skipLast}
          onChange={(event) => {
            const skipLast = event.target.value;
            setDraft({ ...draft, skipLast });
            const parsed = parseWhole(skipLast);
            if (parsed !== null) save({ skip_last: parsed }, 300, "skip_last");
          }}
        />
      </label>
      <label>
        Speed
        <input
          type="number"
          min={0.1}
          step={0.1}
          value={draft.speed}
          onChange={(event) => {
            const speed = event.target.value;
            setDraft({ ...draft, speed });
            const parsed = parseSpeed(speed);
            if (parsed !== null) save({ playback_speed: parsed }, 300, "speed");
          }}
        />
      </label>
      <label>
        Episodes to keep
        <input
          type="number"
          min={1}
          value={draft.count}
          onChange={(event) => {
            const count = event.target.value;
            setDraft({ ...draft, count });
            const parsed = parseCount(count);
            if (parsed !== null) save({ num_episodes: parsed }, 300, "count");
          }}
        />
      </label>
      <div className="cut-ads">
        <CutAdsBadge
          on={effectiveDraftCutAds(props.folders, draft)}
          title={draft.cutAds === null ? folderAdPhrase(props.folders, draft.folder) : showCutAdsTitle(props.show, draft.cutAds)}
          onToggle={() => {
            const cutAds = !effectiveDraftCutAds(props.folders, draft);
            setDraft({ ...draft, cutAds });
            save({ cut_ads: cutAds }, 0, "cut_ads");
          }}
        />
        {draft.cutAds === null ? (
          <span className="hint">{folderAdPhrase(props.folders, draft.folder)}</span>
        ) : (
          <button
            type="button"
            className="quiet"
            onClick={() => {
              setDraft({ ...draft, cutAds: null });
              save({ cut_ads: null }, 0, "cut_ads");
            }}
          >
            Use folder setting
          </button>
        )}
      </div>
    </div>
  );
}

function AddShowDialog(props: {
  folders: string[];
  initialFolder: string;
  enqueue: Save;
  onClose: () => void;
}) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchHit[]>([]);
  const [paste, setPaste] = useState("");
  const [name, setName] = useState("");
  const [feedId, setFeedId] = useState("");
  const [folder, setFolder] = useState(props.initialFolder);
  const [error, setError] = useState("");
  const [looking, setLooking] = useState(false);
  const nameTouched = useRef(false);
  const searchTimer = useRef<number | null>(null);
  const searchGeneration = useRef(0);
  const pasteTimer = useRef<number | null>(null);

  useEffect(() => {
    return () => {
      if (searchTimer.current !== null) window.clearTimeout(searchTimer.current);
      if (pasteTimer.current !== null) window.clearTimeout(pasteTimer.current);
    };
  }, []);

  function choose(hit: SearchHit) {
    if (pasteTimer.current !== null) window.clearTimeout(pasteTimer.current);
    if (searchTimer.current !== null) window.clearTimeout(searchTimer.current);
    searchGeneration.current += 1;
    setResults([]);
    setName(hit.title);
    setFeedId(String(hit.feed_id));
    nameTouched.current = true;
    setError("");
  }

  function scheduleResolve(value: string) {
    if (pasteTimer.current !== null) window.clearTimeout(pasteTimer.current);
    const trimmed = value.trim();
    if (!trimmed) return;
    nameTouched.current = false;
    pasteTimer.current = window.setTimeout(async () => {
      setLooking(true);
      try {
        const resolved = await resolveFeed(trimmed);
        setFeedId(String(resolved.feed_id));
        if (!nameTouched.current) setName(resolved.name);
        setError("");
      } catch (resolveError) {
        setError(resolveError instanceof Error ? resolveError.message : "Lookup failed");
      } finally {
        setLooking(false);
      }
    }, 300);
  }

  return (
    <div className="backdrop" onClick={props.onClose}>
      <form
        className="dialog"
        onClick={(event) => event.stopPropagation()}
        onSubmit={(event) => {
          event.preventDefault();
          const parsed = Number(feedId);
          if (!name.trim() || !Number.isInteger(parsed)) {
            setError("Choose a show or paste a feed URL or id.");
            return;
          }
          void props.enqueue("add-show", (version) =>
            createShow({ version, name: name.trim(), feed_id: parsed, folder }),
          ).then((message) => {
            if (message) setError(message);
            else props.onClose();
          });
        }}
      >
        <div className="dialog-head">
          <h2>Add show</h2>
          <button type="button" className="quiet" onClick={props.onClose}>Close</button>
        </div>
        <label>
          Search Podcast Index
          <input
            value={query}
            placeholder="Show name"
            onChange={(event) => {
              const value = event.target.value;
              setQuery(value);
              if (searchTimer.current !== null) window.clearTimeout(searchTimer.current);
              const generation = searchGeneration.current + 1;
              searchGeneration.current = generation;
              searchTimer.current = window.setTimeout(async () => {
                if (generation !== searchGeneration.current) return;
                if (!value.trim()) {
                  setResults([]);
                  return;
                }
                try {
                  const hits = await searchShows(value.trim());
                  if (generation !== searchGeneration.current) return;
                  setResults(hits);
                  setError("");
                } catch (searchError) {
                  if (generation !== searchGeneration.current) return;
                  setError(searchError instanceof Error ? searchError.message : "Search failed");
                }
              }, 300);
            }}
          />
        </label>
        {results.length > 0 ? (
          <ul className="results">
            {results.map((hit) => (
              <li key={hit.feed_id}>
                <button type="button" onClick={() => choose(hit)}>
                  <strong>{hit.title}</strong>
                  <span>{hit.author} · {hit.feed_id}</span>
                </button>
              </li>
            ))}
          </ul>
        ) : null}
        <label>
          Feed URL or Podcast Index id
          <input
            value={paste}
            placeholder="https://… or 1365758"
            onChange={(event) => {
              const value = event.target.value;
              setPaste(value);
              scheduleResolve(value);
            }}
          />
        </label>
        <label>
          Name
          <input
            value={name}
            onChange={(event) => {
              nameTouched.current = true;
              setName(event.target.value);
            }}
          />
        </label>
        <label>
          Feed id
          <input value={feedId} onChange={(event) => setFeedId(event.target.value)} />
        </label>
        <label>
          Folder
          <select value={folder} onChange={(event) => setFolder(event.target.value)}>
            {props.folders.map((item) => (
              <option key={item} value={item}>{item}</option>
            ))}
          </select>
        </label>
        {looking ? <p className="hint">Looking up that feed…</p> : null}
        {error ? <p className="row-error">{error}</p> : null}
        <button type="submit" className="primary">Add to {folder}</button>
      </form>
    </div>
  );
}

function LocalEpisodes(props: {
  episodes: ArchiveEpisode[];
  ready: boolean;
  error: string;
  playback: Playback;
}) {
  if (props.error) return <p className="row-error local-note">{props.error}</p>;
  if (!props.ready) return <p className="empty-row local-note">Loading episodes…</p>;
  if (props.episodes.length === 0) return <p className="empty-row local-note">No local episodes yet.</p>;
  return (
    <ul className="local-episodes">
      {props.episodes.map((episode) => {
        const current = props.playback.current?.path === episode.path;
        const playing = current && props.playback.playing;
        return (
          <li key={episode.path} className={["local-episode", current ? "current" : "", playing ? "playing" : ""].filter(Boolean).join(" ")}>
            <button
              type="button"
              className="play-mini"
              onClick={() => props.playback.play(episode)}
            >
              {playing ? "Pause" : "Play"}
            </button>
            <span className="file-copy">
              <span className="file-title">{episode.name}</span>
              <span className="meta">Added {formatAge(episode.mtime)}</span>
            </span>
          </li>
        );
      })}
    </ul>
  );
}

function PlayerDock(props: { playback: Playback; shows: Show[] }) {
  const episode = props.playback.current;
  if (!episode) return null;
  const art = episode.show ? props.shows.find((show) => show.name === episode.show)?.image : null;
  const duration = props.playback.duration;
  const ratio = duration > 0 ? Math.min(props.playback.progress, duration) / duration : 0;
  return (
    <section className={`player-dock${props.playback.playing ? "" : " is-paused"}`} aria-label="Player">
      {art ? <ShowArt src={art} /> : <span className="player-art-fallback" aria-hidden="true" />}
      <div className="player-copy">
        <p className="player-title">{episode.name}</p>
        {episode.show ? <p className="player-show">{episode.show}</p> : null}
      </div>
      {props.playback.playing ? null : (
        <button type="button" className="player-close" onClick={props.playback.close} aria-label="Close player">
          <CloseMark />
        </button>
      )}
      <button type="button" className="play-main" onClick={props.playback.toggle} aria-label={props.playback.playing ? "Pause" : "Play"}>
        {props.playback.playing ? <PauseMark /> : <PlayMark />}
      </button>
      <div className="player-scrub">
        <span className="player-time">{formatClock(props.playback.progress)}</span>
        <input
          className="scrub"
          type="range"
          min={0}
          max={duration || 0}
          step={0.1}
          value={Math.min(props.playback.progress, duration || 0)}
          style={{ "--progress": `${ratio * 100}%` } as CSSProperties}
          aria-label="Playback position"
          onChange={(event) => props.playback.seek(Number(event.target.value))}
        />
        <span className="player-time">{formatClock(duration)}</span>
      </div>
    </section>
  );
}

function PlayMark() {
  return (
    <svg className="play-glyph" viewBox="0 0 24 24" aria-hidden="true">
      <path d="M8 5.5v13l11-6.5-11-6.5z" fill="currentColor" />
    </svg>
  );
}

function PauseMark() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M7 5h3.2v14H7V5zm6.8 0H17v14h-3.2V5z" fill="currentColor" />
    </svg>
  );
}

function CloseMark() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M7 7l10 10M17 7L7 17" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
    </svg>
  );
}

function PlayerView(props: {
  shows: Show[];
  episodes: ArchiveEpisode[] | null;
  error: string;
  playback: Playback;
}) {
  const artByShow = new Map(
    props.shows.flatMap((show) => (show.image ? [[show.name, show.image] as const] : [])),
  );
  return (
    <main className="page">
      {props.error ? <p className="banner">{props.error}</p> : null}
      {!props.episodes && !props.error ? <p className="empty">Loading episodes…</p> : null}
      {props.episodes && props.episodes.length === 0 ? <p className="empty">No local episodes yet.</p> : null}
      {props.episodes?.map((episode) => {
        const art = episode.show ? artByShow.get(episode.show) : undefined;
        const playing = props.playback.current?.path === episode.path;
        return (
          <button
            key={episode.path}
            type="button"
            className={`file-row playable${playing ? " playing" : ""}`}
            onClick={() => props.playback.play(episode)}
          >
            {art ? <ShowArt src={art} /> : null}
            <span className="file-copy">
              {episode.show ? <span className="file-show">{episode.show}</span> : null}
              <span className="file-title">{episode.name}</span>
              <span className="meta">
                <span>{episode.folder}</span>
                <span>Added {formatAge(episode.mtime)}</span>
              </span>
            </span>
            <AdsMark kind={episode.kind} />
          </button>
        );
      })}
    </main>
  );
}

function withoutFile(volumes: HeadphoneVolume[] | null, volume: string, path: string): HeadphoneVolume[] | null {
  if (!volumes) return volumes;
  return volumes.map((item) => {
    if (item.name !== volume) return item;
    return {
      ...item,
      folders: item.folders
        .map((folder) => ({
          ...folder,
          files: folder.files.filter((file) => file.path !== path),
        }))
        .filter((folder) => folder.files.length > 0),
    };
  });
}

function HeadphonesView(props: { shows: Show[]; onNotice: (message: string) => void }) {
  const [volumes, setVolumes] = useState<HeadphoneVolume[] | null>(null);
  const [error, setError] = useState("");
  const epoch = useRef(0);
  const reload = useRef<() => void>(() => undefined);

  useEffect(() => {
    let cancelled = false;
    let running = false;
    let again = false;

    async function load() {
      if (cancelled) return;
      if (running) {
        again = true;
        return;
      }
      running = true;
      again = false;
      const seen = epoch.current;
      try {
        const next = await getHeadphones();
        if (cancelled || seen !== epoch.current) return;
        setVolumes(next);
        setError("");
      } catch (loadError) {
        if (!cancelled && seen === epoch.current) {
          setError(loadError instanceof Error ? loadError.message : "Could not read the headphones");
        }
      } finally {
        running = false;
        if (!cancelled && (again || seen !== epoch.current)) {
          again = false;
          void load();
        }
      }
    }

    reload.current = () => {
      void load();
    };
    void load();
    const id = window.setInterval(() => void load(), 1000);
    return () => {
      cancelled = true;
      epoch.current += 1;
      window.clearInterval(id);
    };
  }, []);

  async function remove(volume: string, file: HeadphoneFile) {
    const label = file.show ? `${file.show}: ${file.name}` : file.name;
    if (!window.confirm(`Delete ${label} from ${volume}? The copy on this Mac stays.`)) return;
    epoch.current += 1;
    setVolumes((current) => withoutFile(current, volume, file.path));
    try {
      await deleteHeadphoneFile(volume, file.path);
    } catch (deleteError) {
      if (!(deleteError instanceof ApiError && deleteError.status === 404)) {
        const message = deleteError instanceof Error ? deleteError.message : "Could not delete that file";
        setError(message);
        props.onNotice(message);
        epoch.current += 1;
        reload.current();
        return;
      }
    }
    epoch.current += 1;
    reload.current();
  }

  const artByShow = new Map(
    props.shows.flatMap((show) => (show.image ? [[show.name, show.image] as const] : [])),
  );

  return (
    <main className="page">
      {error ? <p className="banner">{error}</p> : null}
      {volumes && volumes.length === 0 ? (
        <p className="empty">Plug in SWIMBUDS or OpenSwim to see the files on the headphones.</p>
      ) : null}
      {volumes?.map((volume) => (
        <section key={volume.name} className="folder">
          <div className="folder-head">
            <h2>{volume.name}</h2>
          </div>
          {volume.folders.length === 0 ? <p className="empty-row">No audio files on this drive.</p> : null}
          {volume.folders.map((folder) => (
            <div key={folder.name} className="drive-folder">
              <h3>{folder.name}</h3>
              {folder.files.map((file) => {
                const art = file.show ? artByShow.get(file.show) : undefined;
                return (
                  <div key={file.path} className="file-row">
                    {art ? <ShowArt src={art} /> : null}
                    <div className="file-copy" title={file.filename}>
                      {file.show ? <span className="file-show">{file.show}</span> : null}
                      <span className="file-title">{file.name}</span>
                      <span className="meta">
                        <span title={formatMtime(file.posted ?? file.mtime)}>{formatWhen(file)}</span>
                        {file.duration != null ? <span>{formatDuration(file.duration)}</span> : null}
                      </span>
                    </div>
                    <button type="button" className="quiet danger-text" onClick={() => void remove(volume.name, file)}>
                      Delete
                    </button>
                  </div>
                );
              })}
            </div>
          ))}
        </section>
      ))}
    </main>
  );
}

function draftFrom(show: Show): Draft {
  return {
    folder: show.folder,
    skipFirst: String(show.skip_first),
    skipLast: String(show.skip_last),
    speed: String(show.playback_speed),
    count: String(show.num_episodes),
    cutAds: show.cut_ads,
  };
}

function parseWhole(value: string): number | null {
  if (!/^\d+$/.test(value)) return null;
  return Number(value);
}

function parseCount(value: string): number | null {
  if (!/^[1-9]\d*$/.test(value)) return null;
  return Number(value);
}

function parseSpeed(value: string): number | null {
  if (!value.trim()) return null;
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed <= 0) return null;
  return parsed;
}

function CutAdsBadge(props: { on: boolean; title: string; onToggle: () => void }) {
  const label = props.on ? "Cut ads" : "Keep ads";
  return (
    <button
      type="button"
      className={`status cut-ads-toggle ${props.on ? "sliced" : "trimmed"}`}
      aria-pressed={props.on}
      aria-label={label}
      title={props.title}
      onClick={props.onToggle}
    >
      {label}
    </button>
  );
}

function effectiveDraftCutAds(folders: { name: string; cut_ads: boolean }[], draft: Draft): boolean {
  if (draft.cutAds !== null) return draft.cutAds;
  return folders.find((folder) => folder.name === draft.folder)?.cut_ads ?? true;
}

function showCutAdsTitle(show: Show, on: boolean): string {
  const inherited = show.cut_ads === null && on === show.effective_cut_ads;
  if (inherited) {
    return on
      ? `${show.folder} cuts ads, and this show follows it. Click to keep ads for this show.`
      : `${show.folder} keeps ads, and this show follows it. Click to cut ads for this show.`;
  }
  return on ? "This show cuts ads. Click to keep them." : "This show keeps ads. Click to cut them.";
}

function HeadphonesMark() {
  return (
    <svg viewBox="0 0 32 32" aria-hidden="true">
      <path
        d="M9.2 16.4v-1.6a6.8 6.8 0 0 1 13.6 0v1.6"
        fill="none"
        stroke="currentColor"
        strokeWidth="2.4"
        strokeLinecap="round"
      />
      <rect x="6.1" y="15.2" width="4.4" height="8" rx="1.6" fill="currentColor" />
      <rect x="21.5" y="15.2" width="4.4" height="8" rx="1.6" fill="currentColor" />
    </svg>
  );
}

function TrashMark() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path
        d="M9 4.8h6M5.2 7.2h13.6M8.2 7.2l.6 11.4a1.4 1.4 0 0 0 1.4 1.3h3.6a1.4 1.4 0 0 0 1.4-1.3l.6-11.4M10.2 10.2v6M13.8 10.2v6"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function AdsMark(props: { kind: ArchiveEpisode["kind"] }) {
  return (
    <span className={`status ${props.kind}`} title={adsDetail(props.kind)}>
      {adsLabel(props.kind)}
    </span>
  );
}

function adsLabel(kind: ArchiveEpisode["kind"]): string {
  switch (kind) {
    case "sliced":
      return "Ads cut";
    case "trimmed":
      return "Ads kept";
    default: {
      const unexpected: never = kind;
      return unexpected;
    }
  }
}

function adsDetail(kind: ArchiveEpisode["kind"]): string {
  switch (kind) {
    case "sliced":
      return "Ads were removed from this episode.";
    case "trimmed":
      return "This episode kept its ads.";
    default: {
      const unexpected: never = kind;
      return unexpected;
    }
  }
}

function statusLabel(status: ShowStatus): string {
  switch (status) {
    case "preparing":
      return "Preparing";
    case "sliced":
    case "trimmed":
      return "Ads";
    case "not_prepared":
      return "Not prepared";
    default: {
      const unexpected: never = status;
      return unexpected;
    }
  }
}

function statusDetail(status: ShowStatus): string {
  switch (status) {
    case "preparing":
      return "Prepare is running for this show.";
    case "sliced":
      return "The latest episode had its ads removed.";
    case "trimmed":
      return "The latest episode kept its ads. Only the start, end, and speed were applied.";
    case "not_prepared":
      return "No episode has been prepared yet.";
    default: {
      const unexpected: never = status;
      return unexpected;
    }
  }
}

function showOptions(show: Show): string[] {
  const episodes = show.num_episodes === 1 ? "1 episode" : `${show.num_episodes} episodes`;
  return [
    `Start ${formatDuration(show.skip_first)}`,
    `End ${formatDuration(show.skip_last)}`,
    formatSpeed(show.playback_speed),
    episodes,
  ];
}

function folderAdPhrase(folders: { name: string; cut_ads: boolean }[], name: string): string {
  const folder = folders.find((item) => item.name === name);
  if (!folder) return "";
  return folder.cut_ads ? `${folder.name} cuts ads` : `${folder.name} keeps ads`;
}

function formatSpeed(speed: number): string {
  const rounded = Math.round(speed * 10) / 10;
  const text = Number.isInteger(rounded) ? String(rounded) : rounded.toFixed(1);
  return `${text}×`;
}

function formatWhen(file: HeadphoneFile): string {
  if (file.posted != null) return `Posted ${formatPosted(file.posted)} · ${formatAge(file.posted)}`;
  return `Added ${formatAge(file.mtime)}`;
}

function formatPosted(seconds: number): string {
  const date = new Date(seconds * 1000);
  const sameYear = date.getFullYear() === new Date().getFullYear();
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    ...(sameYear ? {} : { year: "numeric" }),
  }).format(date);
}

function formatAge(seconds: number): string {
  const elapsed = Math.max(0, Date.now() / 1000 - seconds);
  if (elapsed < 60) return "just now";
  if (elapsed < 3600) {
    const minutes = Math.floor(elapsed / 60);
    return minutes === 1 ? "1 minute ago" : `${minutes} minutes ago`;
  }
  if (elapsed < 86400) {
    const hours = Math.floor(elapsed / 3600);
    return hours === 1 ? "1 hour ago" : `${hours} hours ago`;
  }
  const days = Math.floor(elapsed / 86400);
  if (days < 14) return days === 1 ? "1 day ago" : `${days} days ago`;
  const weeks = Math.floor(days / 7);
  if (weeks < 8) return weeks === 1 ? "1 week ago" : `${weeks} weeks ago`;
  const months = Math.floor(days / 30);
  if (months < 24) return months === 1 ? "1 month ago" : `${months} months ago`;
  const years = Math.floor(days / 365);
  return years === 1 ? "1 year ago" : `${years} years ago`;
}

function formatClock(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "0:00";
  const whole = Math.floor(seconds);
  const hours = Math.floor(whole / 3600);
  const minutes = Math.floor((whole % 3600) / 60);
  const remainder = whole % 60;
  const sec = String(remainder).padStart(2, "0");
  if (hours > 0) return `${hours}:${String(minutes).padStart(2, "0")}:${sec}`;
  return `${minutes}:${sec}`;
}

function formatDuration(seconds: number): string {
  const whole = Math.max(0, Math.round(seconds));
  const hours = Math.floor(whole / 3600);
  const minutes = Math.floor((whole % 3600) / 60);
  const remainder = whole % 60;
  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 0 && remainder > 0) return `${minutes}m ${remainder}s`;
  if (minutes > 0) return `${minutes}m`;
  return `${remainder}s`;
}

function formatMtime(seconds: number): string {
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(new Date(seconds * 1000));
}

