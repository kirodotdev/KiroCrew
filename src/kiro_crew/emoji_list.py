"""Standard Slack emoji shortcodes for autocomplete."""

# Curated list of ~300 commonly-used standard Slack emojis, grouped by category.
# Custom workspace emojis are fetched from Slack API and merged at runtime.

STANDARD_EMOJIS: list[str] = [
    # Smileys
    "grinning", "smile", "laughing", "joy", "rofl", "wink", "blush",
    "innocent", "heart_eyes", "star_struck", "kissing_heart", "yum",
    "stuck_out_tongue_winking_eye", "zany_face", "hugging_face", "thinking_face",
    "shushing_face", "zipper_mouth_face", "raised_eyebrow", "neutral_face",
    "expressionless", "no_mouth", "smirk", "unamused", "rolling_eyes",
    "grimacing", "lying_face", "relieved", "sleepy", "sleeping",
    "mask", "nerd_face", "sunglasses", "disguised_face", "cowboy_hat_face",
    "exploding_head", "scream", "flushed", "cold_sweat", "sob",
    "cry", "confused", "worried", "slightly_frowning_face", "disappointed",
    "angry", "rage", "sweat", "persevere", "tired_face",
    "skull", "ghost", "alien", "robot_face", "clown_face",
    # Hands & gestures
    "+1", "-1", "thumbsup", "thumbsdown", "ok_hand", "pinching_hand",
    "v", "crossed_fingers", "love_you_gesture", "metal", "call_me_hand",
    "point_left", "point_right", "point_up", "point_down", "point_up_2",
    "raised_hand", "raised_back_of_hand", "wave", "clap", "open_hands",
    "raised_hands", "palms_up_together", "handshake", "pray", "muscle",
    "writing_hand", "selfie", "nail_care", "fist", "punch",
    # People
    "bust_in_silhouette", "busts_in_silhouette", "man_technologist",
    "woman_technologist", "man_shrugging", "woman_shrugging",
    # Hearts & emotions
    "heart", "orange_heart", "yellow_heart", "green_heart", "blue_heart",
    "purple_heart", "black_heart", "white_heart", "broken_heart",
    "sparkling_heart", "heartpulse", "revolving_hearts", "two_hearts",
    "fire", "100", "sparkles", "star", "star2", "dizzy", "boom", "collision",
    # Symbols & marks
    "check", "white_check_mark", "heavy_check_mark", "ballot_box_with_check",
    "x", "negative_squared_cross_mark", "warning", "no_entry", "no_entry_sign",
    "question", "exclamation", "bangbang", "interrobang",
    "red_circle", "orange_circle", "yellow_circle", "green_circle", "blue_circle",
    "large_green_circle", "large_red_square", "large_blue_diamond",
    # Objects & tools
    "bulb", "memo", "pencil2", "pen", "crayon", "mag", "mag_right",
    "flashlight", "wrench", "hammer", "gear", "link", "chains",
    "lock", "unlock", "key", "bell", "no_bell",
    "mega", "loudspeaker", "mute", "speaker", "sound",
    "pushpin", "round_pushpin", "paperclip", "scissors", "triangular_ruler",
    "calendar", "date", "hourglass", "stopwatch", "timer_clock", "alarm_clock",
    "package", "mailbox", "inbox_tray", "outbox_tray", "envelope",
    "bookmark", "label", "clipboard", "file_folder", "open_file_folder",
    "wastebasket", "shield",
    # Tech & computing
    "computer", "desktop_computer", "keyboard", "mouse_three_button",
    "floppy_disk", "cd", "dvd", "battery", "electric_plug",
    "satellite", "joystick",
    # Nature & weather
    "sunny", "cloud", "umbrella", "snowflake", "zap", "rainbow",
    "ocean", "earth_americas", "earth_asia", "earth_africa",
    "deciduous_tree", "evergreen_tree", "cactus", "seedling", "herb",
    "shamrock", "four_leaf_clover", "fallen_leaf", "maple_leaf",
    "mushroom", "cherry_blossom", "rose", "sunflower", "tulip",
    # Animals
    "dog", "cat", "mouse2", "rabbit", "bear", "panda_face",
    "penguin", "bird", "eagle", "owl", "bat", "wolf",
    "fox_face", "unicorn_face", "bee", "bug", "butterfly",
    "snail", "octopus", "whale", "dolphin", "fish", "shark",
    "snake", "dragon", "t_rex", "lobster",
    # Food & drink
    "coffee", "tea", "beer", "beers", "wine_glass", "cocktail",
    "tropical_drink", "champagne", "pizza", "hamburger", "fries",
    "hotdog", "taco", "burrito", "sushi", "ramen",
    "cake", "birthday", "cookie", "doughnut", "ice_cream",
    "apple", "banana", "watermelon", "grapes", "strawberry",
    "avocado", "corn", "hot_pepper", "broccoli",
    # Activities & celebration
    "tada", "confetti_ball", "balloon", "party_popper",
    "trophy", "medal", "first_place_medal", "second_place_medal",
    "crown", "gem", "ring", "ribbon",
    "gift", "ticket", "admission_tickets",
    "soccer", "basketball", "football", "baseball", "tennis",
    "dart", "bowling", "video_game", "chess_pawn",
    # Travel & transport
    "rocket", "airplane", "helicopter", "car", "taxi", "bus",
    "ambulance", "fire_engine", "police_car", "ship", "sailboat",
    "bike", "scooter", "train", "metro",
    # Flags & misc
    "checkered_flag", "triangular_flag_on_post", "crossed_flags",
    "white_flag", "pirate_flag", "rainbow_flag",
    # Common Slack-specific
    "eyes", "eye", "brain", "speech_balloon", "thought_balloon",
    "zzz", "raised_hand_with_fingers_splayed",
    "heavy_plus_sign", "heavy_minus_sign", "heavy_division_sign",
    "heavy_multiplication_x", "infinity",
    "arrow_up", "arrow_down", "arrow_left", "arrow_right",
    "arrow_upper_right", "arrow_lower_right",
    "arrows_counterclockwise", "repeat", "twisted_rightwards_arrows",
    "abc", "capital_abcd", "1234", "symbols", "information_source",
]
