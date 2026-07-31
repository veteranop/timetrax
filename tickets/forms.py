from django import forms

from .models import Comment, Item, WorkNote


class TicketForm(forms.ModelForm):
    class Meta:
        model = Item
        fields = ["title", "description", "client", "priority", "assignee"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 4}),
        }


class WorkNoteForm(forms.ModelForm):
    # Body optional so you can log time-only; hours accepts any decimal.
    body = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 2, "placeholder": "What did you do?"}),
    )
    hours = forms.DecimalField(
        required=False, min_value=0, max_digits=6, decimal_places=2,
        widget=forms.NumberInput(attrs={"step": "any", "placeholder": "0.5 = 30 min"}),
    )

    class Meta:
        model = WorkNote
        fields = ["body", "hours"]

    def clean_hours(self):
        return self.cleaned_data.get("hours") or 0


class CommentForm(forms.ModelForm):
    class Meta:
        model = Comment
        fields = ["body"]
        widgets = {
            "body": forms.Textarea(attrs={"rows": 2, "placeholder": "Add a comment…"}),
        }
