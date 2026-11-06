package com.wirepod.extras;

import android.app.TimePickerDialog;
import android.view.Gravity;
import android.view.View;
import android.widget.CheckBox;
import android.widget.LinearLayout;
import android.widget.RadioButton;
import android.widget.RadioGroup;
import android.widget.Switch;
import android.widget.TextView;
import android.widget.TimePicker;

import org.json.JSONArray;
import org.json.JSONObject;

/**
 * Quiet hours for each day: Vector only wakes up for "Hey Vector", with an optional bedtime (he goes to his
 * charger and sleeps) and no snoring. Kept by the community server (/v1/quiet), which wire-pod sends to the robot.
 */
public class QuietHoursActivity extends Screen {
    private static final String[] DAYS = {"Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"};

    private Switch enabled, snore;
    private RadioGroup bedtime;
    private final CheckBox[] on = new CheckBox[7];
    private final TextView[] start = new TextView[7];
    private final TextView[] end = new TextView[7];
    private TextView status;
    private String esn = "";

    @Override
    protected String title() {
        return "Quiet hours";
    }

    @Override
    protected void build() {
        enabled = sw("Quiet hours");
        note("During quiet hours Vector only wakes up when you say \"Hey Vector\". Touching him, noises, lights, "
                + "picking him up and the app won't wake him, and messages wait until morning.");

        heading("Bedtime");
        bedtime = new RadioGroup(this);
        bedtime.addView(radio(400, "Do nothing", "He keeps doing what he's doing, and sleeps when he's tired."));
        bedtime.addView(radio(401, "Go to sleep", "When quiet hours start he sleeps: on his charger if he's on it, "
                + "otherwise he drives home first."));
        bedtime.check(400);
        body.addView(bedtime);
        note("Either way, if \"Hey Vector\" wakes him during quiet hours, he goes back to sleep a minute after you're done.");

        heading("Sleep");
        snore = sw("Snore at night");

        heading("Days");
        for (int i = 0; i < 7; i++) {
            day(i);
        }
        row("Use Sunday's times for every day", new View.OnClickListener() {
            public void onClick(View v) {
                for (int i = 1; i < 7; i++) {
                    on[i].setChecked(on[0].isChecked());
                    start[i].setText(start[0].getText());
                    end[i].setText(end[0].getText());
                }
            }
        });

        button("Save", new View.OnClickListener() {
            public void onClick(View v) {
                save();
            }
        });
        status = status();

        background(status, "Loading…", new Task() {
            public String run() throws Exception {
                esn = Wp.robot(QuietHoursActivity.this).getString("esn");
                final JSONObject j = Wp.getJson(QuietHoursActivity.this, "/v1/quiet?serial=" + Wp.enc(esn));
                onUi(new Runnable() {
                    public void run() {
                        show(j);
                    }
                });
                return "";
            }
        });
    }

    private Switch sw(String label) {
        Switch s = new Switch(this);
        s.setText(label);
        s.setTextColor(WHITE);
        s.setTextSize(17);
        s.setPadding(dp(4), dp(12), dp(4), dp(8));
        body.addView(s);
        return s;
    }

    private RadioButton radio(int id, String label, String detail) {
        RadioButton r = new RadioButton(this);
        r.setId(id);
        r.setText(label + "\n" + detail);
        r.setTextColor(WHITE);
        r.setPadding(dp(4), dp(8), 0, dp(8));
        return r;
    }

    /** One day: on/off, then its start and end times (tap to change). */
    private void day(final int i) {
        LinearLayout line = new LinearLayout(this);
        line.setGravity(Gravity.CENTER_VERTICAL);
        on[i] = new CheckBox(this);
        on[i].setText(DAYS[i]);
        on[i].setTextColor(WHITE);
        on[i].setChecked(true);
        line.addView(on[i], new LinearLayout.LayoutParams(0, -2, 1f));
        start[i] = time("22:00");
        line.addView(start[i]);
        line.addView(text("  to  ", 15, GREY, false));
        end[i] = time("07:00");
        line.addView(end[i]);
        body.addView(line);
    }

    private TextView time(String value) {
        final TextView t = text(value, 17, GOLD, true);
        t.setPadding(dp(10), dp(10), dp(10), dp(10));
        t.setBackground(box(DARK_GREY, 0));
        t.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                String[] hm = t.getText().toString().split(":");
                new TimePickerDialog(QuietHoursActivity.this, new TimePickerDialog.OnTimeSetListener() {
                    public void onTimeSet(TimePicker p, int h, int m) {
                        t.setText(String.format(java.util.Locale.US, "%02d:%02d", h, m));
                    }
                }, Integer.parseInt(hm[0]), Integer.parseInt(hm[1]), false).show();
            }
        });
        return t;
    }

    private void show(JSONObject j) {
        enabled.setChecked(j.optBoolean("enabled"));
        bedtime.check("sleep".equals(j.optString("bedtime")) ? 401 : 400);
        snore.setChecked(j.optBoolean("snore", true));
        JSONArray days = j.optJSONArray("days");
        for (int i = 0; days != null && i < 7 && i < days.length(); i++) {
            JSONObject d = days.optJSONObject(i);
            on[i].setChecked(d.optBoolean("on"));
            start[i].setText(d.optString("start", "22:00"));
            end[i].setText(d.optString("end", "07:00"));
        }
    }

    private void save() {
        final JSONObject b = new JSONObject();
        try {
            b.put("esn", esn);
            b.put("enabled", enabled.isChecked());
            b.put("bedtime", bedtime.getCheckedRadioButtonId() == 401 ? "sleep" : "nothing");
            b.put("snore", snore.isChecked());
            JSONArray days = new JSONArray();
            for (int i = 0; i < 7; i++) {
                days.put(new JSONObject().put("on", on[i].isChecked())
                        .put("start", start[i].getText().toString()).put("end", end[i].getText().toString()));
            }
            b.put("days", days);
        } catch (Exception e) {
            status.setText(e.toString());
            return;
        }
        background(status, "Saving…", new Task() {
            public String run() throws Exception {
                Wp.postJson(QuietHoursActivity.this, "/v1/quiet", b);
                return "Saved. Vector gets the new settings within a few seconds.";
            }
        });
    }
}
