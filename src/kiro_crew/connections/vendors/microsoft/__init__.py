"""Microsoft-family connector source.

The shared Microsoft Graph runtime base lives under ``graph/``; each Graph-backed
service (SharePoint, Outlook, OneDrive, OneNote, Teams, Excel, Office documents)
is a separate downstream package built on it. Note that ``kiro_crew.teams`` is
the Bot Framework channel transport, NOT the Graph Teams connector -- the two are
unrelated.
"""
