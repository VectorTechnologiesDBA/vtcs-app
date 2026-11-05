package com.wirepod.extras;

import android.app.AlertDialog;
import android.content.DialogInterface;
import android.graphics.Typeface;
import android.view.View;
import android.widget.EditText;
import android.widget.RadioButton;
import android.widget.RadioGroup;
import android.widget.TextView;

import org.json.JSONArray;
import org.json.JSONObject;

/**
 * VIM (Vector Instant Messaging): this robot's add code, the people added with theirs, and sending them a message
 * that their Vector says out loud. Contacts and messages are kept by the community server (/v1/vim).
 */
public class MessagesActivity extends Screen {
    private TextView code, status, addStatus;
    private EditText myName, message, addCode, addName;
    private RadioGroup contacts;
    private JSONArray list = new JSONArray();
    private String esn = "";

    @Override
    protected String title() {
        return "Messages";
    }

    @Override
    protected void build() {
        note("Say \"Hey Vector, text Bob\" and he'll ask what to say, or type a message here. "
                + "Bob's Vector says it out loud.");

        heading("Your add code");
        code = text("······", 34, GREEN, true);
        code.setTypeface(Typeface.MONOSPACE, Typeface.BOLD);
        code.setLetterSpacing(0.2f);
        code.setTextIsSelectable(true);
        code.setPadding(0, dp(4), 0, dp(4));
        body.addView(code);
        note("Give this code to friends so they can add your Vector.");
        myName = field("Your name (what your friends' Vectors call you)", "", false);
        button("Save name", new View.OnClickListener() {
            public void onClick(View v) {
                final String n = myName.getText().toString().trim();
                if (n.length() > 0) {
                    call(status, "Saving…", "name", body("name", n), "Saved.");
                }
            }
        });

        heading("Send a message");
        contacts = new RadioGroup(this);
        body.addView(contacts);
        message = field("Message", "", true);
        button("Send", new View.OnClickListener() {
            public void onClick(View v) {
                final String t = message.getText().toString().trim();
                int i = contacts.getCheckedRadioButtonId() - 300;
                if (t.length() == 0 || i < 0 || i >= list.length()) {
                    status.setTextColor(RED);
                    status.setText(list.length() == 0 ? "Add someone first." : "Pick who to send it to, and type a message.");
                    return;
                }
                final JSONObject to = list.optJSONObject(i);
                call(status, "Sending…", "send", body("to", to.optString("esn"), "text", t),
                        "Sent to " + to.optString("name") + ".");
                message.setText("");
            }
        });
        status = status();
        note("Hold a name to rename or remove them.");

        heading("Add someone");
        addCode = field("Their add code", "", false);
        addName = field("Their name (what you'll call them, e.g. Bob)", "", false);
        button("Add", new View.OnClickListener() {
            public void onClick(View v) {
                String c = addCode.getText().toString().trim();
                if (c.length() == 0) {
                    return;
                }
                call(addStatus, "Adding…", "add", body("code", c, "name", addName.getText().toString().trim()),
                        "Added. Say \"Hey Vector, text " + (addName.getText().toString().trim().length() > 0
                                ? addName.getText().toString().trim() : "them") + "\".");
                addCode.setText("");
                addName.setText("");
            }
        });
        addStatus = status();

        background(status, "Loading…", new Task() {
            public String run() throws Exception {
                esn = Wp.robot(MessagesActivity.this).getString("esn");
                show(Wp.getJson(MessagesActivity.this, "/v1/vim?serial=" + Wp.enc(esn)), true);
                return "";
            }
        });
    }

    /** {"esn": this robot, key: value, ...} */
    private JSONObject body(String... kv) {
        JSONObject b = new JSONObject();
        try {
            b.put("esn", esn);
            for (int i = 0; i + 1 < kv.length; i += 2) {
                b.put(kv[i], kv[i + 1]);
            }
        } catch (Exception e) {
            // only for a null key
        }
        return b;
    }

    /** POST /v1/vim/<action>; the answer (this robot's VIM info) refreshes the screen. */
    private void call(TextView line, String busy, final String action, final JSONObject b, final String done) {
        background(line, busy, new Task() {
            public String run() throws Exception {
                JSONObject j = new JSONObject(Wp.postJson(MessagesActivity.this, "/v1/vim/" + action, b));
                if (j.has("code")) {
                    show(j, false);
                }
                return done;
            }
        });
    }

    private void show(final JSONObject j, final boolean first) {
        onUi(new Runnable() {
            public void run() {
                code.setText(j.optString("code"));
                if (first) {
                    myName.setText(j.optString("name"));
                }
                int checked = contacts.getCheckedRadioButtonId();
                list = j.optJSONArray("contacts") == null ? new JSONArray() : j.optJSONArray("contacts");
                contacts.removeAllViews();
                for (int i = 0; i < list.length(); i++) {
                    final JSONObject c = list.optJSONObject(i);
                    RadioButton r = new RadioButton(MessagesActivity.this);
                    r.setId(300 + i);
                    r.setText(c.optString("name"));
                    r.setTextColor(WHITE);
                    r.setTextSize(17);
                    r.setPadding(dp(4), dp(8), 0, dp(8));
                    r.setOnLongClickListener(new View.OnLongClickListener() {
                        public boolean onLongClick(View v) {
                            edit(c);
                            return true;
                        }
                    });
                    contacts.addView(r);
                }
                if (list.length() == 1) {
                    contacts.check(300);
                } else if (checked >= 300 && checked < 300 + list.length()) {
                    contacts.check(checked);
                }
                if (list.length() == 0) {
                    TextView none = text("No one yet. Add someone with their code below.", 15, GREY, false);
                    contacts.addView(none);
                }
            }
        });
    }

    private void edit(final JSONObject c) {
        final EditText name = new EditText(this);
        name.setText(c.optString("name"));
        new AlertDialog.Builder(this)
                .setTitle(c.optString("name"))
                .setView(name)
                .setPositiveButton("Rename", new DialogInterface.OnClickListener() {
                    public void onClick(DialogInterface d, int w) {
                        call(status, "Saving…", "rename",
                                body("contact", c.optString("esn"), "name", name.getText().toString().trim()), "Renamed.");
                    }
                })
                .setNeutralButton("Remove", new DialogInterface.OnClickListener() {
                    public void onClick(DialogInterface d, int w) {
                        call(status, "Removing…", "remove", body("contact", c.optString("esn")),
                                "Removed " + c.optString("name") + ".");
                    }
                })
                .setNegativeButton("Cancel", null)
                .show();
    }
}
