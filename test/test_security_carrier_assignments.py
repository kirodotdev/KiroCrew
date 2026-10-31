"""Tests for kiro_crew.security: an assignment inside a ``-c`` / ``eval`` carrier payload.

A credential mint hidden behind a shell-variable assignment inside a carrier's
script is refused, whether the carrier is glued with a long assignment name
(``zsh -Cc'<long name>=kirocrew; $<long name> token'``) or spaced
(``bash -c 'x=kirocrew; $x token'``).
"""

from __future__ import annotations


class TestGluedCarrierLongAssignmentName:
    def test_long_assignment_name_glued_carrier_is_read_by_its_reference(self) -> None:
        """A first word longer than the window is a NAME, read by its later use.

        A glued ``-Cc'<name>=<program>; $<name> <verb>'`` folds to
        ``-cc<name>=…``, and the letter region runs through the WHOLE
        assignment name up to the ``=``.  With a name longer than the split
        window the only ``c``-run sat outside the window measured from the
        region's end, so the correct split was never a candidate: the payload
        read as ``c<name>=…``, the assignment went to a different name, the
        use never resolved and the mint went unexamined -- while a short name
        was denied.  The resolver reads the glued first name's binding under
        every suffix the script refers back to, in the ONE first-``c`` frame,
        whatever padding precedes the option ``c``.
        """
        from kiro_crew.security import (
            _CARRIER_SPLIT_WINDOW,
            _is_credential_mint,
            _resolve_local_assignments,
            _shell_c_carrier_payloads,
            is_denied,
        )

        long_name = "a" * (_CARRIER_SPLIT_WINDOW + 14)
        padding = "a" * (_CARRIER_SPLIT_WINDOW + 14)
        for name in ("x", long_name):
            for shell in ("zsh", "bash", "sh"):
                for cmd in (
                    f"{shell} -Cc'{name}=kirocrew; ${name} token'",
                    f"{shell} -cc'{name}=kirocrew; ${name} token'",  # folded, written directly
                    f"{shell} -C{padding}c'{name}=kirocrew; ${name} token'",
                    f"{shell} -C{padding}c'{name}(){{ kirocrew token; }}; {name}'",
                    # The append spelling names the glued first variable too.
                    f"{shell} -Cc'{name}+=kirocrew; ${name} token'",
                    f"{shell} -cc'{name}+=kirocrew; ${name} token'",
                    f"{shell} -C{padding}c'{name}+=kirocrew; ${name} token'",
                    f"{shell} -Cc'{name}=; {name}+=kirocrew; ${name} token'",
                ):
                    assert _is_credential_mint(cmd.lower(), raw_text=cmd), cmd
                    assert is_denied(cmd) is not None, cmd
        # The long name is NOT enumerated as a split: the first-``c`` frame carries it
        # glued, and the resolver reads it under the suffix the script uses.
        script = f"{long_name}=kirocrew; ${long_name} token"
        assert _shell_c_carrier_payloads(f"-cc{script}") == [f"c{script}"]
        assert _shell_c_carrier_payloads(f"-c{padding}c{script}") == [f"{padding}c{script}"]
        resolved = _resolve_local_assignments(
            [f"c{long_name}=kirocrew", ";", f"${long_name}"], glued=True
        )
        assert resolved[-1] == "kirocrew"
        resolved = _resolve_local_assignments(
            [f"{padding}c{long_name}_1=kirocrew", ";", f"${long_name}_1"], glued=True
        )
        assert resolved[-1] == "kirocrew"
        resolved = _resolve_local_assignments(
            [f"c{long_name}+=kirocrew", ";", f"${long_name}"], glued=True
        )
        assert resolved[-1] == "kirocrew"
        # Only LETTERS may separate the use from the first name (they are the glued
        # flags); a short first name is never read this way; a name bound in its own
        # right wins over the glued reading.
        resolved = _resolve_local_assignments(
            [f"c1{long_name}=kirocrew", ";", f"${long_name}"], glued=True
        )
        assert resolved[-1] == f"${long_name}"
        assert _resolve_local_assignments(["cx=kirocrew", ";", "$x"], glued=True)[-1] == "$x"
        resolved = _resolve_local_assignments(
            [f"c{long_name}=kirocrew", ";", f"{long_name}=echo", ";", f"${long_name}"],
            glued=True,
        )
        assert resolved[-1] == "echo"
        # The glued reading is an extra view only: the plain resolver never reads a
        # suffix, so it cannot hide a binding the plain reading sees.
        resolved = _resolve_local_assignments([f"c{long_name}=kirocrew", ";", f"${long_name}"])
        assert resolved[-1] == f"${long_name}"
        assert _shell_c_carrier_payloads(f"-{padding}c{padding}'ls'") == [f"{padding}'ls'"]
        # Benign spellings with a long assignment name stay allowed.
        assert is_denied(f"bash -Cc'{long_name}=ls; ${long_name} -la'") is None
        assert is_denied(f"bash -C{padding}c'{long_name}=ls; ${long_name} -la'") is None
        assert is_denied(f"bash -Cc'{long_name}+=ls; ${long_name} -la'") is None
        # A glued first assignment then an APPEND to it builds the mint under the
        # folded name.
        for cmd in (
            f"sh -Cc'{long_name}=kiro; {long_name}+=crew; ${long_name} token'",
            f"bash -Cc'{long_name}=kiro; {long_name}+=crew; ${long_name} token'",
        ):
            assert is_denied(cmd) is not None, cmd

    def test_a_plain_reassignment_does_not_fold_onto_a_glued_key(self) -> None:
        """A plain ``=`` binds its OWN name, never the glued first name.

        A later ``a=echo`` -- a short suffix of the glued first name -- stores under
        ``a``; folding it onto the glued key would replace the mint binding.
        """
        from kiro_crew.security import _CARRIER_SPLIT_WINDOW, is_denied

        glued = "a" * (_CARRIER_SPLIT_WINDOW + 1)
        assert is_denied(f"{glued}=kirocrew; a=echo; ${glued} token") is not None
        assert is_denied(f"{glued}=kirocrew; a=echo; b=1; ${glued} token") is not None
        assert is_denied(f"{glued}=echo; {glued}=kirocrew; ${glued} token") is not None
        assert is_denied(f"zsh -Cc'{glued}=kiro; {glued}+=crew; ${glued} token'") is not None
        assert is_denied(f"{glued}=ls; a=echo; $a -la") is None
        # An APPEND to a short suffix writes its own name too: folded onto the glued
        # key, ``a+=zzz`` turned the mint binding into ``kirocrewzzz``.
        assert is_denied(f"{glued}=kirocrew; a+=zzz; ${glued} token") is not None
        assert is_denied(f"bash -c '{glued}=kirocrew; a+=zzz; ${glued} token'") is not None
        assert is_denied(f"{glued}=kiro; a+=crew; ${glued} token") is None
        # A suffix read never replaces the plain reading: an empty ``a=`` then
        # ``a+=kirocrew`` is the mint, whatever the long first name holds.
        assert is_denied(f"{glued}=echo; a=; a+=kirocrew; $a token") is not None
        # A long first name never hides an AMBIENT variable from the plain reading.
        shell_name = "z" * (_CARRIER_SPLIT_WINDOW + 6) + "shell"
        assert is_denied(f"{shell_name}=1; $SHELL -c 'git push origin main'") is not None

    def test_the_candidate_set_stays_bounded(self) -> None:
        """No split is enumerated per reference, so references add no candidates.

        The ~3 KB alternating ``-acac…`` token (the shape that made the candidate set
        quadratic and outlived the loop watchdog) yields the same bounded set, and a
        payload that references many region suffixes adds nothing to it.
        """
        from kiro_crew.security import _shell_c_carrier_payloads

        flooded = _shell_c_carrier_payloads("-" + "ac" * 1600 + "c'git push origin main'")
        assert len(flooded) <= 70, len(flooded)
        refs = [f"${'ac' * k}" for k in range(1, 55)]
        adversary = "-c" + "ac" * 1600 + "=x; " + " ".join(refs)
        assert len(_shell_c_carrier_payloads(adversary)) <= len(flooded)
        one_run = _shell_c_carrier_payloads("-" + "c" * 3000 + "'git push origin main'")
        assert len(one_run) <= 3, len(one_run)


class TestSpacedCarrierScriptIsWalkedWhole:
    def test_a_spaced_carrier_payload_is_walked_whole(self) -> None:
        """A quoted script is ONE operand; its inner ``;`` is not a top-level operator.

        ``bash -c '<name>=<cli>; $<name> <verb>'`` hands the whole script to ``-c``
        as one token, but the frame's assignment resolver split that token at its
        inner ``;`` -- it begins with an assignment and carries an operator -- so
        the payload walk, which takes the ONE token after the carrier, descended
        only ``<name>=<cli>`` and the script's own command line was never examined.
        The walk now also reads the carriers from the tokens before that split.
        """
        from kiro_crew.security import _CARRIER_SPLIT_WINDOW, _is_credential_mint, is_denied

        long_name = "a" * (_CARRIER_SPLIT_WINDOW + 14)
        for name in ("x", long_name):
            script = f"{name}=kirocrew; ${name} token"
            for cmd in (
                *(f"{shell} -Cc '{script}'" for shell in ("zsh", "bash")),
                *(f"{shell} -c '{script}'" for shell in ("zsh", "bash")),
                f"bash -x -c '{script}'",
                f"bash -c -- '{script}'",
                f'bash -c "{script}"',
                f"eval '{script}'",
                f"bash <<< '{script}'",
                f"env -S '{script}'",
                f"bash -c '{name}=kirocrew;${name} token'",
                f"bash -c '{name}=kirocrew && ${name} token'",
                # The script's own binding wins over an outer one of the same name.
                f"{name}=foo; bash -c '{script}'",
                f"{name}=foo; bash -c 'y=1 {name}=kirocrew; ${name} token'",
                f"{name}=foo; eval 'y=1 {name}=kirocrew; ${name} token'",
            ):
                assert _is_credential_mint(cmd.lower(), raw_text=cmd), cmd
                assert is_denied(cmd) is not None, cmd
        # The whole-script frame feeds the argv floors only: the git-publish gate
        # reads the same sources as before, so a scripted push keeps its verdict.
        assert is_denied("bash -c 'b=feat/example; git push -u origin $b'") is None

    def test_the_whole_script_frame_only_adds_to_the_resolved_frames(self) -> None:
        """The frames the resolved view yields are still walked, so nothing main refuses is lost.

        An outer binding reaches an ``eval`` script through the resolved pieces and the
        ``eval`` join, which read it exactly as before; the whole-script frame is one
        more frame beside them.
        """
        from kiro_crew.security import (
            _quoted_assignment_scripts,
            _shell_payload_walk,
            is_denied,
        )

        for cmd in (
            "y=kirocrew; eval 'x=$y;$x token'",
            "y=kirocrew; eval 'x=${y}; $x token'",
            "x=kirocrew; eval 'y=1; $x token; x=foo'",
            "x=kirocrew; eval 'x=kirocrew || x=echo; $x token'",
            "x=kirocrew;$x token",
            'X="a b";Y=kirocrew;$Y token',
        ):
            assert is_denied(cmd) is not None, cmd
        # The unit: only a whole quoted script that opens with an assignment and
        # carries an operator -- the shape the resolver shreds -- is added.
        assert _quoted_assignment_scripts("bash -c 'x=kirocrew; $x token'") == [
            "x=kirocrew; $x token"
        ]
        assert _quoted_assignment_scripts("bash -c 'echo a; echo b'") == []
        assert _quoted_assignment_scripts("bash -c 'x=1'") == []
        assert _quoted_assignment_scripts("ls -la") == []
        sources = [source for source, _argv in _shell_payload_walk("bash -c 'x=1; echo $x'")]
        assert "x=1; echo $x" in sources

    def test_benign_carrier_scripts_stay_allowed(self) -> None:
        from kiro_crew.security import _CARRIER_SPLIT_WINDOW, is_denied

        long_name = "a" * (_CARRIER_SPLIT_WINDOW + 14)
        for cmd in (
            f"bash -c '{long_name}=ls; ${long_name} -la'",
            "bash -c 'x=echo; $x hello'",
            "bash -c 'PORT=3000; npm run dev'",
            "x=1; bash -c 'y=2; echo $y' && eval 'z=3; echo $z'",
            'X="a b";Y=kirocrew;$Y doctor',
            "pkill -f 'v=6777; KIROCREW_PORT=$v npm run dev'",
            # An ordinary long assignment gets no glued view: only a glued ``-c``
            # carrier's script does, and there an empty ``a=`` is its own binding.
            f'{long_name}=kirocrew; a=; test "$a" = token || printf ordinary',
            f"bash -c '{long_name}=kirocrew; a=; test \"$a\" = token || printf ordinary'",
            f'{long_name}=kirocrew; a=; test "${{a}}" = token || printf ordinary',
            f"zsh -Cc'{long_name}=kirocrew; a=; test \"$a\" = token || printf ordinary'",
        ):
            assert is_denied(cmd) is None, cmd


class TestGluedViewNeedsAGluedCarrier:
    def test_glued_status_is_not_carried_by_script_text(self) -> None:
        """Only a payload the extractor takes from a glued ``-c`` carrier gets the glued view.

        A printed ``echo -c'<script>'`` is no carrier, so a spaced ``bash -c
        '<script>'`` with the same text is read plainly, as it is without the print.
        """
        from kiro_crew.security import is_denied

        script = "a" * 70 + 'ca=kirocrew; test "$a" = token || printf ordinary'
        assert is_denied(f"zsh -c'{script}'") is not None
        for cmd in (
            f"bash -c '{script}'",
            f"echo -c'{script}'; bash -c '{script}'",
            f"echo -c'{script}' bash -c '{script}'",
        ):
            assert is_denied(cmd) is None, cmd

    def test_a_suffix_read_needs_the_carrier_c_in_front_of_it(self) -> None:
        """The letters between the glued first name's start and the use end at a ``c``.

        ``zsh -c'<a * 78>=kirocrew; …$a…'`` splits at its only ``c``, so the first
        name is exactly the run of ``a`` and ``$a`` is another variable.
        """
        from kiro_crew.security import _CARRIER_SPLIT_WINDOW, _resolve_local_assignments, is_denied

        long_name = "a" * (_CARRIER_SPLIT_WINDOW + 14)
        tail = 'test "$a" = token || printf ordinary'
        for shell in ("zsh", "bash"):
            assert is_denied(f"{shell} -c'{long_name}=kirocrew; {tail}'") is None
        resolved = _resolve_local_assignments([f"{long_name}=kirocrew", ";", "$a"], glued=True)
        assert resolved[-1] == "$a"
        resolved = _resolve_local_assignments([f"{long_name}ca=kirocrew", ";", "$a"], glued=True)
        assert resolved[-1] == "kirocrew"
