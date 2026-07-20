import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react"
import ReactMarkdown from "react-markdown"
import remarkGfm from "remark-gfm"
import {
  Check,
  ChevronDown,
  CircleAlert,
  Database,
  LoaderCircle,
  Moon,
  Plus,
  Send,
  Sun,
  TriangleAlert,
  Wrench,
  X,
} from "lucide-react"

import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { Textarea } from "@/components/ui/textarea"
import { cn } from "@/lib/utils"

// --- API types (mirror backend/app.py SSE contract) -------------------------

type HealthCheck = { name: string; ok: boolean; detail: string }
type Health = { ready: boolean; checks: HealthCheck[]; message: string }

type SSEEvent =
  | { type: "text"; text: string }
  | { type: "thinking"; text: string }
  | { type: "tool_use"; id: string; name: string; input: unknown }
  | { type: "tool_result"; id: string; ok: boolean; summary: string }
  | { type: "error"; message: string }
  | { type: "done"; duration_ms: number }

// --- Transcript model -------------------------------------------------------

type TextBlockT = { kind: "text"; text: string }
type ThinkingBlockT = { kind: "thinking"; text: string }
type ToolBlockT = {
  kind: "tool"
  id: string
  name: string
  input: unknown
  status: "running" | "ok" | "error"
  summary?: string
}
type ErrorBlockT = { kind: "error"; text: string }
type Block = TextBlockT | ThinkingBlockT | ToolBlockT | ErrorBlockT

type Message =
  | { id: string; role: "user"; text: string }
  | { id: string; role: "assistant"; blocks: Block[]; durationMs?: number }

// --- helpers ----------------------------------------------------------------

const uid = () =>
  typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : Math.random().toString(36).slice(2)

// Turn "mcp__snowflake__execute_query" into "execute_query".
function prettyToolName(name: string): string {
  const parts = name.split("__")
  return parts[parts.length - 1] || name
}

function toolInputSummary(input: unknown): string | null {
  if (input == null) return null
  if (typeof input === "string") return input
  if (typeof input === "object") {
    const obj = input as Record<string, unknown>
    // Prefer a SQL / query-ish field if present.
    for (const key of ["sql", "query", "statement"]) {
      if (typeof obj[key] === "string") return obj[key] as string
    }
    const entries = Object.entries(obj)
      .filter(([, v]) => v != null && v !== "")
      .map(([k, v]) => `${k}: ${typeof v === "string" ? v : JSON.stringify(v)}`)
    return entries.length ? entries.join("  ·  ") : null
  }
  return String(input)
}

// --- Markdown ---------------------------------------------------------------

function Markdown({ children }: { children: string }) {
  return (
    <div className="prose-chat">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          table: ({ children }) => (
            <div className="my-3 w-full overflow-x-auto rounded-lg border border-border">
              <table className="w-full border-collapse text-sm">{children}</table>
            </div>
          ),
          thead: ({ children }) => (
            <thead className="bg-muted/60">{children}</thead>
          ),
          th: ({ children }) => (
            <th className="border-b border-border px-3 py-2 text-left font-semibold whitespace-nowrap">
              {children}
            </th>
          ),
          td: ({ children }) => (
            <td className="border-b border-border/60 px-3 py-2 align-top">
              {children}
            </td>
          ),
          tr: ({ children }) => (
            <tr className="even:bg-muted/20">{children}</tr>
          ),
          a: ({ children, href }) => (
            <a
              href={href}
              target="_blank"
              rel="noreferrer"
              className="text-primary underline underline-offset-2"
            >
              {children}
            </a>
          ),
          code: ({ className, children, ...props }) => {
            const isBlock = /\n/.test(String(children))
            if (isBlock) {
              return (
                <pre className="my-3 overflow-x-auto rounded-lg border border-border bg-muted/50 p-3 text-sm">
                  <code className={className} {...props}>
                    {children}
                  </code>
                </pre>
              )
            }
            return (
              <code
                className="rounded bg-muted px-1.5 py-0.5 font-mono text-[0.85em]"
                {...props}
              >
                {children}
              </code>
            )
          },
          ul: ({ children }) => (
            <ul className="my-2 list-disc space-y-1 pl-5">{children}</ul>
          ),
          ol: ({ children }) => (
            <ol className="my-2 list-decimal space-y-1 pl-5">{children}</ol>
          ),
          p: ({ children }) => (
            <p className="my-2 leading-relaxed first:mt-0 last:mb-0">
              {children}
            </p>
          ),
          h1: ({ children }) => (
            <h1 className="mt-4 mb-2 text-xl font-semibold">{children}</h1>
          ),
          h2: ({ children }) => (
            <h2 className="mt-4 mb-2 text-lg font-semibold">{children}</h2>
          ),
          h3: ({ children }) => (
            <h3 className="mt-3 mb-1.5 text-base font-semibold">{children}</h3>
          ),
          blockquote: ({ children }) => (
            <blockquote className="my-2 border-l-2 border-border pl-3 text-muted-foreground">
              {children}
            </blockquote>
          ),
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  )
}

// --- Tool chip --------------------------------------------------------------

function ToolChip({ block }: { block: ToolBlockT }) {
  const [open, setOpen] = useState(false)
  const detail = toolInputSummary(block.input)
  const statusColor =
    block.status === "error"
      ? "text-destructive"
      : block.status === "ok"
        ? "text-emerald-500"
        : "text-sky-600 dark:text-sky-400"

  return (
    <div className="my-1.5 overflow-hidden rounded-lg border border-sky-300/70 bg-sky-100/80 text-sm dark:border-sky-900/50 dark:bg-sky-950/30">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-sky-200/60 dark:hover:bg-sky-900/30"
      >
        <Wrench className="size-3.5 shrink-0 text-sky-600 dark:text-sky-400" />
        <span className="font-mono text-xs font-medium">
          {prettyToolName(block.name)}
        </span>
        <span className={cn("ml-auto flex items-center gap-1 text-xs", statusColor)}>
          {block.status === "running" && (
            <LoaderCircle className="size-3.5 animate-spin" />
          )}
          {block.status === "ok" && <Check className="size-3.5" />}
          {block.status === "error" && <X className="size-3.5" />}
          {block.summary ??
            (block.status === "running" ? "running…" : block.status)}
        </span>
        {detail && (
          <ChevronDown
            className={cn(
              "size-3.5 shrink-0 text-muted-foreground transition-transform",
              open && "rotate-180"
            )}
          />
        )}
      </button>
      {open && detail && (
        <div className="border-t border-sky-300/50 bg-white/50 px-3 py-2 dark:border-sky-900/50 dark:bg-background/40">
          <pre className="overflow-x-auto font-mono text-xs whitespace-pre-wrap text-slate-600 dark:text-muted-foreground">
            {detail}
          </pre>
        </div>
      )}
    </div>
  )
}

// --- Message bubbles --------------------------------------------------------

function UserBubble({ text }: { text: string }) {
  return (
    <div className="flex justify-end">
      <div className="max-w-[85%] rounded-2xl rounded-br-sm bg-primary px-4 py-2.5 whitespace-pre-wrap text-primary-foreground dark:bg-[#87cefa] dark:text-slate-900">
        {text}
      </div>
    </div>
  )
}

function ThinkingBlock({ text }: { text: string }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="my-1.5 text-sm">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex items-center gap-1.5 text-xs text-muted-foreground/70 italic hover:text-muted-foreground"
      >
        <ChevronDown
          className={cn("size-3 transition-transform", open && "rotate-180")}
        />
        thinking
      </button>
      {open && (
        <div className="mt-1 border-l-2 border-border pl-3 text-xs whitespace-pre-wrap text-muted-foreground/80">
          {text}
        </div>
      )}
    </div>
  )
}

function AssistantBubble({ message }: { message: Extract<Message, { role: "assistant" }> }) {
  const empty = message.blocks.length === 0
  return (
    <div className="flex justify-start">
      <div className="flex max-w-[92%] gap-3">
        <div className="mt-1 flex size-7 shrink-0 items-center justify-center rounded-full bg-muted text-muted-foreground">
          <Database className="size-4" />
        </div>
        <div className="min-w-0 flex-1 rounded-2xl rounded-bl-sm bg-[#f0f9ff] px-4 py-2.5 text-slate-900 dark:bg-muted dark:text-foreground">
          {empty && (
            <div className="flex items-center gap-1.5 py-1 text-muted-foreground">
              <LoaderCircle className="size-4 animate-spin" />
              <span className="text-sm">Thinking…</span>
            </div>
          )}
          {message.blocks.map((b, i) => {
            if (b.kind === "text")
              return <Markdown key={i}>{b.text}</Markdown>
            if (b.kind === "thinking")
              return <ThinkingBlock key={i} text={b.text} />
            if (b.kind === "tool") return <ToolChip key={i} block={b} />
            return (
              <div
                key={i}
                className="my-1.5 flex items-start gap-2 rounded-lg border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
              >
                <CircleAlert className="mt-0.5 size-4 shrink-0" />
                <span className="whitespace-pre-wrap">{b.text}</span>
              </div>
            )
          })}
          {message.durationMs != null && (
            <div className="mt-1 text-[11px] text-muted-foreground/60">
              {(message.durationMs / 1000).toFixed(1)}s
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

// --- Setup / diagnostics screen ---------------------------------------------

function SetupScreen({
  health,
  onRetry,
  retrying,
}: {
  health: Health
  onRetry: () => void
  retrying: boolean
}) {
  return (
    <div className="mx-auto flex w-full max-w-xl flex-1 flex-col justify-center px-4 py-10">
      <Card className="gap-4 p-6">
        <div className="flex items-center gap-3">
          <div className="flex size-10 items-center justify-center rounded-full bg-amber-500/15 text-amber-500">
            <TriangleAlert className="size-5" />
          </div>
          <div>
            <h1 className="text-lg font-semibold">Setup needed</h1>
            <p className="text-sm text-muted-foreground">{health.message}</p>
          </div>
        </div>
        <ul className="flex flex-col gap-2">
          {health.checks.map((c) => (
            <li
              key={c.name}
              className="flex items-start gap-3 rounded-lg border border-border p-3"
            >
              <div
                className={cn(
                  "mt-0.5 flex size-5 shrink-0 items-center justify-center rounded-full",
                  c.ok
                    ? "bg-emerald-500/15 text-emerald-500"
                    : "bg-destructive/15 text-destructive"
                )}
              >
                {c.ok ? (
                  <Check className="size-3.5" />
                ) : (
                  <X className="size-3.5" />
                )}
              </div>
              <div className="min-w-0">
                <div className="text-sm font-medium">{c.name}</div>
                <div className="text-xs break-words text-muted-foreground">
                  {c.detail}
                </div>
              </div>
            </li>
          ))}
        </ul>
        <Button onClick={onRetry} disabled={retrying} className="w-full">
          {retrying && <LoaderCircle className="animate-spin" />}
          Re-check
        </Button>
      </Card>
    </div>
  )
}

// --- App --------------------------------------------------------------------

const SUGGESTIONS = [
  "What tables are in the AIRBNB database?",
  "Describe the schema of MY_FIRST_DBT_MODEL",
  "Preview the first 10 rows of AIRBNB.DBT_SCHEMA.MY_FIRST_DBT_MODEL",
]

export default function App() {
  const [health, setHealth] = useState<Health | null>(null)
  const [healthLoading, setHealthLoading] = useState(true)
  const [messages, setMessages] = useState<Message[]>([])
  const [input, setInput] = useState("")
  const [streaming, setStreaming] = useState(false)
  const [dark, setDark] = useState(true)
  // Model + effort are required before the first prompt; remembered across reloads.
  const [model, setModel] = useState<string>(
    () => localStorage.getItem("snowchat.model") ?? ""
  )
  const [effort, setEffort] = useState<string>(
    () => localStorage.getItem("snowchat.effort") ?? ""
  )
  const ready = !!model && !!effort

  useEffect(() => {
    if (model) localStorage.setItem("snowchat.model", model)
  }, [model])
  useEffect(() => {
    if (effort) localStorage.setItem("snowchat.effort", effort)
  }, [effort])

  const session = useMemo(() => uid(), [])
  const scrollRef = useRef<HTMLDivElement>(null)
  const abortRef = useRef<AbortController | null>(null)

  // Theme -> toggle .dark on <html>
  useEffect(() => {
    document.documentElement.classList.toggle("dark", dark)
  }, [dark])

  // Health check
  const checkHealth = useCallback(async () => {
    setHealthLoading(true)
    try {
      const res = await fetch("/api/health")
      const data = (await res.json()) as Health
      setHealth(data)
    } catch (e) {
      setHealth({
        ready: false,
        message: "Could not reach the backend at /api/health.",
        checks: [
          {
            name: "Backend server",
            ok: false,
            detail: String(e instanceof Error ? e.message : e),
          },
        ],
      })
    } finally {
      setHealthLoading(false)
    }
  }, [])

  useEffect(() => {
    void checkHealth()
  }, [checkHealth])

  // Autoscroll to newest
  useLayoutEffect(() => {
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [messages])

  // Mutate the in-flight assistant message.
  const updateAssistant = useCallback(
    (id: string, fn: (m: Extract<Message, { role: "assistant" }>) => Extract<Message, { role: "assistant" }>) => {
      setMessages((prev) =>
        prev.map((m) => (m.id === id && m.role === "assistant" ? fn(m) : m))
      )
    },
    []
  )

  const applyEvent = useCallback(
    (id: string, ev: SSEEvent) => {
      switch (ev.type) {
        case "text":
          updateAssistant(id, (m) => {
            const blocks = [...m.blocks]
            const last = blocks[blocks.length - 1]
            if (last && last.kind === "text") {
              blocks[blocks.length - 1] = { kind: "text", text: last.text + ev.text }
            } else {
              blocks.push({ kind: "text", text: ev.text })
            }
            return { ...m, blocks }
          })
          break
        case "thinking":
          updateAssistant(id, (m) => {
            const blocks = [...m.blocks]
            const last = blocks[blocks.length - 1]
            if (last && last.kind === "thinking") {
              blocks[blocks.length - 1] = { kind: "thinking", text: last.text + ev.text }
            } else {
              blocks.push({ kind: "thinking", text: ev.text })
            }
            return { ...m, blocks }
          })
          break
        case "tool_use":
          updateAssistant(id, (m) => ({
            ...m,
            blocks: [
              ...m.blocks,
              {
                kind: "tool",
                id: ev.id,
                name: ev.name,
                input: ev.input,
                status: "running",
              },
            ],
          }))
          break
        case "tool_result":
          updateAssistant(id, (m) => ({
            ...m,
            blocks: m.blocks.map((b) =>
              b.kind === "tool" && b.id === ev.id
                ? { ...b, status: ev.ok ? "ok" : "error", summary: ev.summary }
                : b
            ),
          }))
          break
        case "error":
          updateAssistant(id, (m) => ({
            ...m,
            blocks: [...m.blocks, { kind: "error", text: ev.message }],
          }))
          break
        case "done":
          updateAssistant(id, (m) => ({ ...m, durationMs: ev.duration_ms }))
          break
      }
    },
    [updateAssistant]
  )

  const send = useCallback(
    async (prompt: string) => {
      const text = prompt.trim()
      if (!text || streaming || !model || !effort) return
      setInput("")
      const assistantId = uid()
      setMessages((prev) => [
        ...prev,
        { id: uid(), role: "user", text },
        { id: assistantId, role: "assistant", blocks: [] },
      ])
      setStreaming(true)

      const controller = new AbortController()
      abortRef.current = controller

      try {
        const res = await fetch("/api/chat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ prompt: text, session, model, effort }),
          signal: controller.signal,
        })
        if (!res.ok || !res.body) {
          applyEvent(assistantId, {
            type: "error",
            message: `Request failed (HTTP ${res.status}).`,
          })
          return
        }

        const reader = res.body.getReader()
        const decoder = new TextDecoder()
        let buffer = ""

        for (;;) {
          const { done, value } = await reader.read()
          if (done) break
          buffer += decoder.decode(value, { stream: true })
          // SSE frames are separated by a blank line.
          let sep: number
          while ((sep = buffer.indexOf("\n\n")) !== -1) {
            const frame = buffer.slice(0, sep)
            buffer = buffer.slice(sep + 2)
            for (const line of frame.split("\n")) {
              const trimmed = line.trimStart()
              if (!trimmed.startsWith("data:")) continue
              const payload = trimmed.slice(5).trim()
              if (!payload) continue
              try {
                applyEvent(assistantId, JSON.parse(payload) as SSEEvent)
              } catch {
                // ignore malformed frame
              }
            }
          }
        }
      } catch (e) {
        if (!controller.signal.aborted) {
          applyEvent(assistantId, {
            type: "error",
            message: String(e instanceof Error ? e.message : e),
          })
        }
      } finally {
        setStreaming(false)
        abortRef.current = null
      }
    },
    [applyEvent, session, streaming, model, effort]
  )

  const newChat = useCallback(async () => {
    abortRef.current?.abort()
    setMessages([])
    setStreaming(false)
    try {
      await fetch("/api/reset", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session }),
      })
    } catch {
      // best-effort
    }
  }, [session])

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault()
      void send(input)
    }
  }

  // --- render ---------------------------------------------------------------

  const header = (
    <header className="flex shrink-0 items-center gap-3 border-b border-border px-4 py-3">
      <div className="flex size-8 items-center justify-center rounded-lg bg-primary text-primary-foreground">
        <Database className="size-4" />
      </div>
      <div className="min-w-0">
        <h1 className="truncate text-sm font-semibold">SnowChat</h1>
        <p className="truncate text-xs text-muted-foreground">
          Ask questions about your Snowflake data
        </p>
      </div>
      <div className="ml-auto flex items-center gap-1">
        <Button
          variant="ghost"
          size="icon"
          onClick={() => setDark((d) => !d)}
          aria-label="Toggle theme"
          title="Toggle theme"
        >
          {dark ? <Sun className="size-4" /> : <Moon className="size-4" />}
        </Button>
        <Button
          variant="outline"
          size="sm"
          onClick={() => void newChat()}
          disabled={messages.length === 0 && !streaming}
        >
          <Plus className="size-4" />
          New chat
        </Button>
      </div>
    </header>
  )

  let body: React.ReactNode
  if (healthLoading && !health) {
    body = (
      <div className="flex flex-1 items-center justify-center text-muted-foreground">
        <LoaderCircle className="mr-2 size-5 animate-spin" />
        Checking backend…
      </div>
    )
  } else if (health && !health.ready) {
    body = (
      <SetupScreen
        health={health}
        onRetry={() => void checkHealth()}
        retrying={healthLoading}
      />
    )
  } else {
    body = (
      <>
        <div ref={scrollRef} className="flex-1 overflow-y-auto">
          <div className="mx-auto w-full max-w-3xl px-4 py-6">
            {messages.length === 0 ? (
              <div className="flex flex-col items-center gap-6 py-16 text-center">
                <div className="flex size-14 items-center justify-center rounded-2xl bg-muted text-muted-foreground">
                  <Database className="size-7" />
                </div>
                <div>
                  <h2 className="text-lg font-semibold">
                    What would you like to know?
                  </h2>
                  <p className="mt-1 text-sm text-muted-foreground">
                    Query your Snowflake warehouse in plain English.
                  </p>
                </div>
                <div className="flex w-full max-w-md flex-col gap-2">
                  {SUGGESTIONS.map((s) => (
                    <button
                      key={s}
                      type="button"
                      onClick={() => void send(s)}
                      disabled={!ready}
                      className="rounded-lg border border-border px-3 py-2 text-left text-sm text-muted-foreground transition-colors hover:bg-muted/60 hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      {s}
                    </button>
                  ))}
                  {!ready && (
                    <p className="text-center text-xs text-amber-500">
                      Choose a model and effort below to begin.
                    </p>
                  )}
                </div>
              </div>
            ) : (
              <div className="flex flex-col gap-5">
                {messages.map((m) =>
                  m.role === "user" ? (
                    <UserBubble key={m.id} text={m.text} />
                  ) : (
                    <AssistantBubble key={m.id} message={m} />
                  )
                )}
              </div>
            )}
          </div>
        </div>

        <div className="shrink-0 border-t border-border px-4 py-3">
          <div className="mx-auto w-full max-w-3xl">
            <div className="mb-2 flex flex-wrap items-center gap-2">
              <select
                value={model}
                onChange={(e) => setModel(e.target.value)}
                aria-label="Model"
                className="rounded-lg border border-border bg-card px-2.5 py-1.5 text-xs text-foreground focus:outline-none focus-visible:border-ring"
              >
                <option value="" disabled>
                  Model…
                </option>
                <option value="haiku">Haiku · fast, cheap</option>
                <option value="sonnet">Sonnet · balanced</option>
                <option value="opus">Opus · most capable</option>
              </select>
              <select
                value={effort}
                onChange={(e) => setEffort(e.target.value)}
                aria-label="Effort"
                className="rounded-lg border border-border bg-card px-2.5 py-1.5 text-xs text-foreground focus:outline-none focus-visible:border-ring"
              >
                <option value="" disabled>
                  Effort…
                </option>
                <option value="low">Effort: low</option>
                <option value="medium">Effort: medium</option>
                <option value="high">Effort: high</option>
                <option value="xhigh">Effort: xhigh</option>
                <option value="max">Effort: max</option>
              </select>
              {model === "haiku" && effort && (
                <span className="text-[11px] text-muted-foreground/60">
                  effort has no effect on Haiku
                </span>
              )}
            </div>
            <div className="relative flex items-end gap-2 rounded-2xl border border-border bg-card p-2 shadow-sm focus-within:border-ring">
              <Textarea
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={onKeyDown}
                placeholder={
                  ready
                    ? "Ask about your Snowflake data…  (Enter to send, Shift+Enter for newline)"
                    : "Choose a model and effort above to begin…"
                }
                rows={1}
                className="max-h-40 min-h-0 flex-1 resize-none border-0 bg-transparent px-2 py-1.5 shadow-none focus-visible:ring-0 dark:bg-transparent"
              />
              <Button
                size="icon"
                onClick={() => void send(input)}
                disabled={!input.trim() || streaming || !ready}
                aria-label="Send"
                className="rounded-xl bg-[#87cefa] text-slate-900 hover:bg-[#5ab4f0] dark:bg-[#87cefa] dark:text-slate-900 dark:hover:bg-[#5ab4f0]"
              >
                {streaming ? (
                  <LoaderCircle className="size-4 animate-spin" />
                ) : (
                  <Send className="size-4" />
                )}
              </Button>
            </div>
            <p className="mt-1.5 text-center text-[11px] text-muted-foreground/60">
              Read-only connection · results shown as tables · switching model or effort
              starts a fresh conversation
            </p>
          </div>
        </div>
      </>
    )
  }

  return (
    <div className="flex h-svh flex-col bg-background text-foreground">
      {header}
      {body}
    </div>
  )
}
