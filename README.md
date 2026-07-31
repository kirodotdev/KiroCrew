# KiroCrew

Open-source personal AI agent that runs on your own machine. Chat from Slack, a
web dashboard, or the CLI; run multi-step tasks unattended; schedule cron jobs;
persist memory across sessions. **[What's New](CHANGELOG.md)**

```
CLI / Slack / Dashboard → KiroCrew gateway → kiro-cli (ACP) → LLM + MCP tools
```

KiroCrew orchestrates the **[kiro-cli](https://kiro.dev)** agent over the
[Agent Client Protocol](https://github.com/zed-industries/agent-client-protocol)
(ACP), adding multi-session management, persistent memory, scheduling, and a web
UI on top of it.

## Quick Start

```bash
# One-line install (macOS / Linux) — prebuilt wheel, SHA-256 verified
curl -fsSL https://download.crew.kiro.dev/cli.sh | sh -s -- --channel nightly

# Then configure and launch
kirocrew setup        # interactive wizard
kirocrew gateway      # opens http://localhost:5476
```

Or build from source:

```bash
git clone https://github.com/kirodotdev/KiroCrew.git && cd KiroCrew
cd website && npm install && npm run build && cd ..
pip install -e .
kirocrew setup && kirocrew gateway
```

The dashboard guides first-time users through installing kiro-cli and completing
sign-in. Run `kirocrew doctor` to verify everything is wired up.

### Docker: run the gateway as a container

For always-on servers (Slack/Discord bots, remote dashboards) the gateway
ships as a multi-arch image on GHCR:

The package is private for now. Authenticate to GHCR with an account that has
package access before pulling; see **[docs/DOCKER.md](docs/DOCKER.md)**.

```bash
docker run -d --name kirocrew \
  -p 127.0.0.1:5476:5476 \
  -v kirocrew-home:/home/kirocrew \
  ghcr.io/kirodotdev/kirocrew:stable
```

See **[docs/docker.md](docs/docker.md)** for first-run login, channel
credentials, tags (`stable` / `insider` / `nightly` / pinned versions), and
the container security model.

See the **[Getting Started guide](docs/getting-started.md)** for the full
walkthrough, including Ollama setup for memory embeddings and all installation
options.

> **Platforms:** macOS, Linux, and Windows (native). See
> [docs/windows-install.md](docs/windows-install.md) for Windows-specific steps.

## What It Does

| Surface | Description |
|---------|-------------|
| **Web Dashboard** | Multi-session chat, memory explorer, cron manager, app store |
| **Slack DM** | Each thread = isolated AI session with full tool access |
| **Desktop App** | Electron wrapper — no Python/npm needed for end users |
| **CLI** | `kirocrew chat`, `kirocrew run TASK.md`, `kirocrew cron`, `kirocrew spawn` |

### Key Capabilities

- **Autonomous tasks** — run multi-step specs unattended (`kirocrew run TASK.md`)
- **Cron scheduling** — recurring jobs with timezone, jitter, and per-job timeouts
- **Subagent orchestration** — spawn parallel background agents
- **Persistent memory** — preferences, projects, and daily history survive restarts
- **Self-learning** — corrections persist as lessons injected into all future sessions
- **App platform** — build and install apps that extend KiroCrew (App Store + SDK)
- **Security** — OS sandbox, credential redaction, 137 denied-command patterns, governance model
- **MCP tools** — auto-discover and manage any MCP server
- **Knowledge Library** — ingest docs/code into a searchable graph
- **Voice** — optional STT/TTS (Piper local, or AWS via `[voice]` extra)

## Running 24/7

```bash
kirocrew service install    # systemd (Linux) or launchd (macOS)
kirocrew service status
```

For remote hosts, see [docs/remote-desktop-setup.md](docs/remote-desktop-setup.md).

## Configuration

Config lives at `~/.kiro/crew/config.json` — manage via `kirocrew config get/set/edit`.

```json
{
  "agent": { "provider": "acp", "approval_mode": "interactive", "sandbox": "auto" },
  "session": { "timeout_secs": 1800 },
  "dashboard": { "bot_name": "KiroCrew" },
  "slack": { "command": "kirocrew" }
}
```

Dashboard port: `KIROCREW_PORT` env var (default `5476`).
Credentials: `~/.kiro/crew/.env` — `SLACK_APP_TOKEN`, `SLACK_BOT_TOKEN`, `KIROCREW_OWNER_ID`.

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `AcpTimeoutError` | Confirm `kiro-cli` is on PATH and logged in; `kirocrew setup --agent-only --clean` to reset MCP config |
| Memory search not working | Install Ollama + `ollama pull qwen3-embedding:0.6b`; run `kirocrew doctor` |
| Slack not connecting | Slack is optional — dashboard works without it. See [docs/slack-setup.md](docs/slack-setup.md) |
| MCP server broken | `kirocrew setup --agent-only --clean` rebuilds from scratch |

## Documentation

| Document | What it covers |
|----------|---------------|
| **[docs/getting-started.md](docs/getting-started.md)** | Full installation walkthrough and first steps |
| [docs/features.md](docs/features.md) | Complete feature reference |
| [docs/project-architecture.md](docs/project-architecture.md) | System architecture with diagrams |
| [docs/install.md](docs/install.md) | All build/install methods (source, wheel, desktop app) |
| [docs/release-process.md](docs/release-process.md) | How releases are cut: branches, channels, versions |
| [docs/security-deep-dive.md](docs/security-deep-dive.md) | Security architecture |
| [docs/memory-architecture.md](docs/memory-architecture.md) | Memory system design |
| [docs/mcp-architecture.md](docs/mcp-architecture.md) | MCP server management |
| [docs/app-kit/getting-started.md](docs/app-kit/getting-started.md) | App Kit developer guide |
| [docs/slack-setup.md](docs/slack-setup.md) | Slack app creation and setup |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Development workflow and PR guidelines |
| [CHANGELOG.md](CHANGELOG.md) | Release history |

## Development

```bash
pip install -e ".[voice]"    # editable backend install
cd website && npm install    # frontend deps

# Quality gate
black src && isort src && flake8 src && mypy src && pytest
```

See [CONTRIBUTING.md](CONTRIBUTING.md) and [AGENTS.md](AGENTS.md) for full guidelines.

## Contributors

KiroCrew was made possible by its internal community — **494 Amazon employees** who supported the project and shipped its code. This is that founding group; as KiroCrew grows in the open, we look forward to many more contributors joining them. Thank you to everyone who helped make this tool possible:

Bolin Chen, Zejiang Guo, Zezhen Xu, Simon Meyffret, Raymond Chen, Nick Bowers, Akim Akimov, Joe Pontone, Patrick Gao, Krish Dhasmana, Hoang Phan, Chen Tong, Yusheng Xu, Hugo Costa, Ben Grubin, Robert Noack, Rohan Khanderia, Luke Ely, Aidan Mackey, Stan Tian, Alec Douglas, James Joseph, Ethan Levine, Nick Papadopoulos, Erik Schweiss, Vitor Durante, Abhishek Mitra, Lanxiao Bai, Gabe Sanchez, Quan Nguyen, Lane Ambrose, Dan Dagayev, Tony Hardie, Nikhil Menon, Vamil Gandhi, Toby Wong, Di Wu, Aswin Damodar, Bocheng Wu, Chetan Chaku, Maksym Yachnyi, Matthew Barnum, Gavin Tse, Chen Yang Lho, Maninder Singh, Chris McMillon, David Fayerman, Naoya Ishikawa, Eduardo Vencovsky, Ezzat Qupty, Shreyas Bhise, Vishal Sreekrishnan, Swapnil Dixit, Mark Lord, Bharath Janyavula, Tyger Hugh, Jiahao Guo, Luca Chang, Yuliang Qiao, Shihao Wang, Joshua Yeung, Roman Ivanov, Bhavana Chinthalapally, Beau Taylor-Ladd, Christopher Huk, Kishore Baskar, Sugavanesh Balasubramanian, Parimal Deshmukh, Gregory Liu, Nansong Yi, Teodor Oprescu, Zhuoyu Li, Graham Roberts, Dinesh Jayapalan, Xu Deng, David Schlessman, Madhur Bajaj, David Hickox, Vishal Mawandia, Peter Vu, Angelo Yu, Uday Prakash, Yuta Tsuji, Sypher Su, Rohit Jose, Pedro Barrios, Yohanes Setiawan, Arpit Vyas, Connor LoPresti, Oscar Smith-Sieger, Sam Oldak, Kotaro Inoue, Shao-Cheng Wang, Rohan Kapadia, Robert Zhang, Arjun Soota, Himakireeti Konda, Jingjin Wei, Matt Pierringer, Jimmy Kilpatrick, Greg Rebholz, Yongbo Xiao, Kejian Wang, Gregory Chapman, Wilson Wu, Ahmed Hassanin, Chaoneng Quan, Siddartha  B V, John Law, Udit Tumuluri, Brent Naylor, Shuya Sawa, Rabinarayan Patra, Minglong Pan, Jianwen Liu, Joel Studevant, Eric Zhang, Greyson Nevins-Archer, August Vilakia, Arpan Banerjee, Rikiya Tsukidate, Yao Bian, Qusai Hussein, Zifeng Xia, Mustafa Onur AYDIN, Adam Doussan, Mikhail Kuznetsov, Tianxiang Xu, Justin Zhang, Kiavash Samadi, Adam Duncan, Rohit Mehra, Finn Haddon, Sean Iamartino, Akshit Desai, Mohammed Elansary, Matthew Nguyen, Axel Vidales, Huan He, Fei Ma, Jingchao Cao, Milos Chaloupka, Helena Stafford, John Espenhahn, Arturo Acuaviva, Hao Xu, Raghav Bhardwaj, Eric Muessel, Curtis Demerah, Dan McClain, Puneeth Nanjundaswamy, Sudhamsu Manne, Shashwat Srivastava, Eric Hays, Satheesh Prabhakaran, Nathan Beals, Krunal Patel, Yashwanth Korla, Tomas Rodriguez Sanchez, Vaibhav Bhatia, Matthieu Dufour, Mike Mayer, Sean Whipple, Dinesh Mathan, Luca Bruera, Marvellous Adedapo, Aryaman Pathania, Ravi Teja Kondisetty, Shayan Yaseen, Reece Bailey, Kyle Seaman, Koushik Ginjupally, Matt McLeod, Arnaldo Garcia, Thomas Lane, Mihir Dhamankar, Sam Cuthbertson, Nirav Adunuthula, David Lee, Thiago Andrade, Tian ZHANG, Vineeth Chinthala, Saif Rahman, Cole Whitley, Emmanuella Dasilva-Domingos, Nihal Singh, Kenneth Harrison, Ashwin Menon, Alex Truong, Ben Bloschock, Selena Wang, Amit Menon, Caillin Bathern, Naveen Adarsh Petla, Joel Blumenthal, Joshua Chang, Chris Boomhower, Matthew Pope, Takahiro Ishii, Yu Zhang, Swapnil Gaikwad, Chris Wundram, Emmanuel Okonkwo, Dhaval Soneji, Mohammed Madni Vaid, Sungjin Yoo, Carter Trpik, Shubham Gupta, David Qian, lili liu, Keshav Kumar Prabhakharan, Vasanth Subramanian, Yehui Zhang, yagna gurjala, Omar Abu Mukh, William Randall, Luis Gabriel Lima, Bobby Earl, Dallin Kooyman, Kevin Goldberg, Nitan Singh, Chen Qiu, Faizan Ali, Rishabh Agrawal, Lysander Hernandez, Emma Zhou, Barrett Karson, Ariana Morgan, Namra Alkeshbhai Saheba, Jason Sirota, Lipeng Yang, Rony Jacob John, Yifan Liu, Nick Gonzales, Maxwell Schroder, Mark Asp, David Ney Abarca, Alex Avance, Chengxi Li, Jaden Yuros, Anthony Orozco, Goutham Manjunatha, Alex Jones, Giovanni Viviani, Luu Tran, Saurav Gupta, Petter Nilsson, Rohan Rajeev, Beau Bright, Lin Zhu, Parikshit Desai, Anirudh Narayanan, Roberto Matarrita Arce, Xinyu Zhao, Tyst Marin, Nate Eklund, Marc Shelton, Pranshu Ranakoti, Dayong Li, Anchit Thakur, William Bowditch, Trevor Liberty, Matthew Muncy, Zach Akin-Amland, Abe Diaz, George Coll, Sebastian Sun, Nishant Srivastava, John Li, Ryan Reich, Zach Herridge, Kushal Jain, Jake Gordon, Tyler Barkley, Marcus Mann, Nathan L. Burns, Shailesh Agrawal, Himanish Kaul, Mariam Alaidi, Imran Baig, Giridhar Shyam Sankararaman, Jake Nocentino, Stephane Robin, Angelo Yang, Vishal Sahoo, Jack Bandon, Aiden Gaines, Leonard Al-Qaseer, Ian Auger-Juul, Juan Segura, Saran Kota, Johnny Mastin, Paul Davis, Vasudeva H, Evan Stenger, Leo Zhadanovsky, Setul Patel, Jiacheng Wang, Michael Viscardi, Hung Vu, Addison Tustin, Filippo Galli, Andrew Janzen, Rittik Gautam, Landon Coe, Khaled Sarieddine, Doruk, Yueyang Mi, Amit Chowdhary, Amr Saleh, Chenying Han, Dhaivat Patel, Avi Mikhli, Jaya Kasiraj, Mathieu Pelletan, Abhishek Sharma, Filip Godina, Viren Khatri, Qiong Liu, Parwinder Singh, Nagabharan Nagendran, Dima Sitnikov, Geet Sawhney, Abhishek Dhameja, Chanon Sinitskul, Brian Thomas, Edward Riede, Shawn Li, Alexander Yuan, SIMING DENG, Marcello Silva, Nani, Jin Cheng, Martin Rowan, Rob Chahin, David Van Winkle, Gavin Mealy, Zhe Lv, Srihari Attuluri, Xuecong Zang, Anmol Saxena, Shubhranshu Kumar, Rohan Kumar, Paxton Tomooka, Tao Jiang, Felipe Barajas, Shuli He, Sandip Dutta, Shuolei Jin, Mujahed Syed, Apoorv Srivastava, Kunal Raut, Raghu Burukunte, Shubham Agrawal, FuChen, Projjol Banerji, Jeff Neuberger, Kyjauna Marshall, Noufal Edappanoli, Wei Wei, Tomasz Lauda, Yu Cheng, Kan Zhu, Anant Kaushik, Spencer Zhang, Balaji S, Spandan Agrawal, Kyle Helmick, Pramod Dudhi, Nitin Kanigicharla, Phillip Gong, Atharwa Adawadkar, Chris Paton, Ishan Mishra, Piyush Galphat, Di Wu, Francesco Falcone, Alexander Shen, Joao Miguel, Adi Sridharan, Derek Wilson, Will Maillard, Roman Sandler, Weinan Si, Austin Goddard, Gilhong Min, Sivan Cooperman, Grant Gollier, Jim Hill, Kevin Zuern, Amir Naghibi, connor marr, Louay Morsi, Kellen Jia, Nolan Clayton, Rob Stevens, Sai Chaitanya Manchikatla, Christopher Tyndall, Nischal Kumar, Warren Bui, Chad Bailey, Manish Kumar Gupta, Jamie Gao, Lachlan Lindsay, Matthew Dwyer, Jake Zhao, Jatin Dewani, Roberto Cidade, Bhargav Mistry, Zhongkai Liu, Akash Shrestha, Alexander Blom, Chris Raley, Serena Tan, Artem Pliasunov, Chance Rebholz, Liam Wirth, Sergey Chebotarev, Zeiad Zaf, David Ramos, Ayan Das, Shameem PK, Weibin Zeng, Rahul Dabas, Indika Pathirage, Moshe Yakovson, Anjan Agarwala, Jiayi Zhang, Zihong Hao, Abhishek Aryan, Jacob Morgan, Manuel Chavez, Wenli Yan, Johnny Xue, Albert Huang, Kaiwei Luo, Alex Yelle, Hugo Wen, Jaya Prakash Reddy Gade, Lakshman sai Donavan, Mert Hizli, Anthony Dominianni, Chris Mendis, Purlaksh, Qinghua Gao, Rochak Gupta, Jonathan Cox, Qifeng Huang, Sujoy Datta, Nikitha Tejpal, Prutha Shouche, Tim Lee, Vinitra Ramasubramaniam, Vivek Sayyaparaju, Albin Shrestha, Bojin Li, Gautam Mishra, Kai Mitsuzawa, Kaique Govani, Nagarajesh Lakshmanan, ShotaroKataoka, Thomas Ricatte, Zhaolong Zhang, Albert Achtenberg, Isaac Weaver, Amulya Sahoo, MJ, Sajal Narang, Rohit Ingle, Shelby Hagman, Venkatesh Babu Ayyallu Rajan, Matt Cohen, Paul McKissock, Zhengfei Ji, Abhishek Shasthry, Amad Salmon, Artem Krivonos, Arvind Srinath Kumar, Aziz Saifuddin, Casey Huggins, Jackie Ly, Justin Treece, Lester Lee, Luke Jung, Manish Patel, Rob Wolinski, Siddhant Jain, Sugan Kumar, Wenyu Yang, Andrew Golightly, Arshdeep Takkar, Daisy Dazhen, Justin Bess, Stif Spear Subba.

## License

See [LICENSE](LICENSE).
