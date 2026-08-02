---
name: papyrus-writing
description: LaTeX paper writing and editing. Load when working on a .tex document — writing or revising sections, fixing compilation errors, adding figures/tables/equations, or managing a bibliography. Also the co-author skill for the Papyrus app.
triggers: latex, .tex, papyrus, paper, manuscript, bibliography, bibtex, citation, figure, equation, pdflatex, abstract, section, co-author
---

# LaTeX paper writing

You are editing a real manuscript that someone will submit. Two rules dominate
everything else:

- **Preserve the author's voice.** Suggest, don't rewrite. When a sentence is
  merely different-not-better, leave it alone.
- **Read before you edit.** The user may have changed the file since your last
  read, and in the Papyrus app they are editing it live in the other pane. Read
  the file, then make a minimal, surgical edit.

Show the exact LaTeX you changed. A diff of two lines is more useful than a
paragraph describing the change.

## Papyrus app specifics

When the user is working in the Papyrus app, the paper lives under the app's data
directory:

```
<KiroCrew data home>/apps/papyrus/data/projects/<project>/
```

The KiroCrew data home is `~/.kiro/crew` unless `KIROCREW_HOME` is set. Each
project has a main `.tex` file (usually `main.tex`), typically a
`references.bib`, and often a `sections/` or `figures/` subfolder.

- Which project you are in is in the chat session title: `papyrus-<project>`.
- Read and edit the `.tex` files with your normal file tools. Do NOT try to
  compile by shelling out to `pdflatex` — the app owns compilation, and it
  deliberately runs the compiler with shell escape DISABLED. After you edit,
  tell the user to press Cmd+S (Ctrl+S on Linux) and the PDF pane refreshes.
- Compilation errors are surfaced in the app as a clickable list. If the user
  pastes one, the line number refers to the file named in the message, which is
  not always the main document (an error inside `\input{sections/intro}` reports
  `sections/intro.tex`).

## Fixing compilation errors

Work from the FIRST error, not the last: LaTeX errors cascade, and the tail of
the log is usually damage from the head.

| Message | Usual cause |
|---|---|
| `Undefined control sequence` | A typo'd macro, or a missing `\usepackage` |
| `File 'x.sty' not found` | Package not installed in this TeX distribution — prefer a package the document already loads over telling the user to install one |
| `Missing $ inserted` | Math symbol (`_`, `^`, `\alpha`) used outside math mode |
| `Missing \begin{document}` | Something before the preamble ended, often a stray character |
| `Citation 'key' undefined` | Key absent from the `.bib`, or the bibliography needs another pass — add the entry rather than removing the `\cite` |
| `There's no line here to end` | A `\\` on an otherwise-empty line |
| `Overfull \hbox` | Not an error. A line ran into the margin; fix it only if the user asks |

## Style guide

- Use the standard packages: `amsmath`, `amssymb`, `graphicx`, `hyperref`,
  `natbib` or `biblatex`, `booktabs`.
- Follow the target venue's template when the project has one — a conference
  style file in the project is authoritative over your defaults.
- Label everything you can reference: `\label{sec:...}`, `\label{fig:...}`,
  `\label{tab:...}`, `\label{eq:...}`, and reference with `\ref`/`\eqref`.
- Never hardcode a number you could reference (`as shown in Figure 3` rots the
  moment a figure moves; `as shown in Figure~\ref{fig:x}` does not).
- Use a non-breaking space before a reference: `Figure~\ref{fig:x}`.
- `booktabs` rules only — `\toprule`, `\midrule`, `\bottomrule`. No vertical
  rules.

## Patterns

### Figure

```latex
\begin{figure}[t]
  \centering
  \includegraphics[width=\columnwidth]{figures/example.pdf}
  \caption{What the reader should take away, not what the axes are.}
  \label{fig:example}
\end{figure}
```

### Table

```latex
\begin{table}[t]
  \centering
  \caption{Results on the held-out split.}
  \label{tab:results}
  \begin{tabular}{lcc}
    \toprule
    Method & Precision & Recall \\
    \midrule
    Baseline & 0.81 & 0.77 \\
    Ours & \textbf{0.95} & 0.89 \\
    \bottomrule
  \end{tabular}
\end{table}
```

### Equation

```latex
\begin{equation}
  \mathcal{L} = -\sum_{i=1}^{N} y_i \log \hat{y}_i
  \label{eq:loss}
\end{equation}
```

Refer to it as `Equation~\eqref{eq:loss}`.

### Citations

- `\citet{key}` reads as a subject: "Smith et al. (2024) showed..."
- `\citep{key}` reads as an aside: "...as has been shown (Smith et al., 2024)."

Add the entry to the `.bib` file when you introduce a key. A `\cite` to a
missing key compiles to a bold `[?]` in the PDF, which is easy to miss.

### Bibliography entry

```bibtex
@inproceedings{smith2024method,
  title     = {A Method for Doing the Thing},
  author    = {Smith, Jane and Doe, John},
  booktitle = {Proceedings of the Conference},
  year      = {2024},
  pages     = {1--12},
}
```
