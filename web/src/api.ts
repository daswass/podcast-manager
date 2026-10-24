export type ShowStatus = "preparing" | "sliced" | "trimmed" | "not_prepared";

export type Folder = {
  name: string;
  cut_ads: boolean;
};

export type Show = {
  name: string;
  feed_id: number;
  folder: string;
  skip_first: number;
  skip_last: number;
  playback_speed: number;
  num_episodes: number;
  cut_ads: boolean | null;
  effective_cut_ads: boolean;
  cut_ads_inherited: boolean;
  status: ShowStatus;
  error: string | null;
  image: string | null;
  url: string | null;
  posted: number | null;
  on_headphones: boolean | null;
};

export type JobActivity = "downloading" | "copying" | "both" | "running" | null;

export type Library = {
  version: string;
  folders: Folder[];
  shows: Show[];
  prepare_busy: boolean;
  prepare_reason: string | null;
  headphone_volume: string | null;
  job: JobActivity;
};

export type SearchHit = {
  title: string;
  author: string;
  feed_id: number;
  url: string;
};

export type ResolvedShow = {
  name: string;
  feed_id: number;
  author: string;
  url: string;
};

export type HeadphoneFile = {
  name: string;
  show: string | null;
  filename: string;
  path: string;
  mtime: number;
  posted: number | null;
  duration: number | null;
};

export type HeadphoneFolder = {
  name: string;
  files: HeadphoneFile[];
};

export type HeadphoneVolume = {
  name: string;
  folders: HeadphoneFolder[];
};

export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
  });
  const body = (await response.json()) as { error?: string; detail?: string };
  if (!response.ok) {
    throw new ApiError(response.status, body.error || body.detail || response.statusText);
  }
  return body as T;
}

export function getLibrary(): Promise<Library> {
  return request<Library>("/api/library");
}

export function createFolder(version: string, name: string): Promise<Library> {
  return request<Library>("/api/folders", {
    method: "POST",
    body: JSON.stringify({ version, name }),
  });
}

export function patchFolder(
  name: string,
  body: { version: string; name?: string; cut_ads?: boolean; index?: number },
): Promise<Library> {
  return request<Library>(`/api/folders/${encodeURIComponent(name)}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export function deleteFolder(name: string, version: string): Promise<Library> {
  return request<Library>(`/api/folders/${encodeURIComponent(name)}?version=${encodeURIComponent(version)}`, {
    method: "DELETE",
  });
}

export function createShow(body: {
  version: string;
  name: string;
  feed_id: number;
  folder: string;
}): Promise<Library> {
  return request<Library>("/api/shows", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export type ShowPatch = {
  folder?: string;
  skip_first?: number;
  skip_last?: number;
  playback_speed?: number;
  num_episodes?: number;
  cut_ads?: boolean | null;
};

export function patchShow(feedId: number, body: ShowPatch & { version: string }): Promise<Library> {
  return request<Library>(`/api/shows/${feedId}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export function deleteShow(feedId: number, version: string): Promise<Library> {
  return request<Library>(`/api/shows/${feedId}?version=${encodeURIComponent(version)}`, {
    method: "DELETE",
  });
}

export function searchShows(query: string): Promise<SearchHit[]> {
  return request<{ results: SearchHit[] }>(`/api/search?q=${encodeURIComponent(query)}`).then(
    (body) => body.results,
  );
}

export function resolveFeed(query: string): Promise<ResolvedShow> {
  return request<ResolvedShow>("/api/resolve", {
    method: "POST",
    body: JSON.stringify({ query }),
  });
}

export function getHeadphones(): Promise<HeadphoneVolume[]> {
  return request<{ volumes: HeadphoneVolume[] }>("/api/headphones").then((body) => body.volumes);
}

export function deleteHeadphoneFile(volume: string, path: string): Promise<void> {
  return request<{ ok: boolean }>("/api/headphones", {
    method: "DELETE",
    body: JSON.stringify({ volume, path }),
  }).then(() => undefined);
}

export function prepareShow(feedId: number): Promise<Library> {
  return request<Library>(`/api/shows/${feedId}/prepare`, { method: "POST" });
}
